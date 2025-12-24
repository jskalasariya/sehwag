"""
Sehwag Strategy - Clean Modular Implementation
===============================================
Multi-leg breakout strategy with clean architecture (supports any index).

Key Features:
✓ Fetch high/low and expiry ONCE at startup
✓ Each leg runs in its own thread
✓ WebSocket-based monitoring with REST API fallback
✓ DB persistence for positions and events
✓ Breakout condition checked at leg entry time
✓ Generic implementation for NIFTY, SENSEX, BANKNIFTY, etc.
"""

import logging
import time
import threading
from typing import Dict, List, Optional
from datetime import datetime
import pytz

from .market_data import MarketDataManager
from .order_manager import OrderManager
from .position_manager import PositionManager
from .persistence_manager import SehwagPersistence
from .logging_manager import get_leg_logger, close_leg_logger

logger = logging.getLogger(__name__)


def is_market_open(tz=pytz.timezone('Asia/Kolkata')) -> bool:
    """Check if market is currently open"""
    return True
    now = datetime.now(tz)
    current_time = now.time()

    # Market hours: 09:15 to 15:30
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0).time()
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0).time()

    # Check if weekend
    if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False

    return market_open <= current_time <= market_close


class LegState:
    """Represents state of a single leg"""

    def __init__(self, leg_num: int, config: Dict, lot_size: int = 75, strategy_defaults: Dict = None):
        self.leg_num = leg_num
        self.name = config.get('name', f'Leg {leg_num}')
        self.config = config

        # Strategy-level defaults for cascading configuration
        self.strategy_defaults = strategy_defaults or {}

        # Entry/Exit times
        self.entry_time: Optional[datetime] = None
        self.exit_time: Optional[datetime] = None

        # Position data
        self.symbol: Optional[str] = None
        self.entry_price: Optional[float] = None
        self.entry_direction: Optional[str] = None  # "CE" or "PE"

        # Calculate quantity: lot_size × lot × lot_multiplier
        # lot: leg-level (default: 1), lot_multiplier: strategy-level (default: 1)
        lot = config.get('lot', 1)
        lot_multiplier = strategy_defaults.get('lot_multiplier', 1)
        self.quantity: int = lot_size * lot * lot_multiplier
        self.entry_order_id: Optional[str] = None
        self.sl_order_id: Optional[str] = None  # Track SL order on broker
        self.profit_target_order_id: Optional[str] = None  # Track profit target order on broker

        # Risk management - leg-level takes precedence over strategy-level
        # CRITICAL FIX: Remove hardcoded 7.0 default to prevent config cascade
        initial_sl = config.get('initial_sl_pct', strategy_defaults.get('initial_sl_pct'))
        self.initial_sl_pct: float = initial_sl if initial_sl is not None else 7.0  # Only use 7.0 as last resort
        self.current_sl: Optional[float] = None
        self.highest_price: Optional[float] = None

        # Trailing SL tracking
        self.last_sl_trail_level: float = 0.0  # Last profit % when SL was trailed

        # Profit lock tracking
        self.first_lock_achieved: bool = False  # Track if first lock (P1) was hit
        self.profit_exit_target: Optional[float] = None  # Current profit exit target %
        self.last_trail_level: float = 0.0  # Last profit % when exit was trailed

        # Status
        self.is_active: bool = False
        self.exit_reason: Optional[str] = None
        self.exit_price: Optional[float] = None

    def calculate_pnl(self, current_price: float) -> tuple[float, float]:
        """Calculate P&L"""
        # Compute P&L based only on stored entry price and quantity. Do NOT rely on
        # `is_active` here because this method is called during the exit flow where
        # `is_active` may already be cleared. Return safe defaults if entry_price
        # is missing or invalid.
        if self.entry_price is None:
            return 0.0, 0.0

        try:
            entry = float(self.entry_price)
            exitp = float(current_price)
        except Exception:
            return 0.0, 0.0

        qty = int(self.quantity) if getattr(self, 'quantity', None) is not None else 0

        price_change = exitp - entry
        pnl = price_change * qty
        pnl_pct = (price_change / entry * 100.0) if entry != 0 else 0.0

        return pnl, pnl_pct


class SehwagStrategy:
    """Main strategy orchestrator - clean and modular (supports any index)"""

    def __init__(self, client, config: Dict, websocket_client=None):
        """
        Initialize strategy

        Args:
            client: OpenAlgo API client
            config: Configuration dictionary
            websocket_client: Optional WebSocket client
        """
        self.client = client
        self.config = config
        self.websocket_client = websocket_client
        self.tz = pytz.timezone(config.get('strategy', {}).get('timezone', 'Asia/Kolkata'))

        # Strategy parameters
        self.underlying = config.get('strategy', {}).get('underlying', 'NIFTY')

        # Derive strategy name from underlying (e.g., "NIFTY" → "nifty_sehwag")
        self.strategy_name = config.get('strategy', {}).get('strategy_name', f"{self.underlying.lower()}_sehwag")

        self.strike_diff = config.get('strategy', {}).get('strike_diff', 50)
        self.lot_size = config.get('strategy', {}).get('lot_size', 75)
        self.lot_multiplier = config.get('strategy', {}).get('lot_multiplier', 1)
        self.option_exchange = config.get('strategy', {}).get('option_exchange', 'NFO')
        self.instrument_type = config.get('strategy', {}).get('instrument_type', 'options')

        # Wait & Trade parameters (with leg-level override support)
        self.wait_trade_threshold_pct = config.get('strategy', {}).get('wait_trade_threshold_pct', 3.0)
        self.wait_trade_timeout_seconds = config.get('strategy', {}).get('wait_trade_timeout_seconds', 300)

        # Breakout Distance Delay parameters (with leg-level override support)
        self.breakout_diff_threshold = config.get('strategy', {}).get('breakout_diff_threshold')
        self.breakout_diff_delay_seconds = config.get('strategy', {}).get('breakout_diff_delay_seconds', 60)

        # Performance tuning - configurable sleep intervals
        self.wait_trade_check_interval = float(config.get('strategy', {}).get('wait_trade_check_interval', 0.5))
        self.monitor_check_interval = float(config.get('strategy', {}).get('monitor_check_interval', 0.1))
        self.monitor_check_interval_no_ws = float(config.get('strategy', {}).get('monitor_check_interval_no_ws', 1.0))
        self.error_retry_interval = float(config.get('strategy', {}).get('error_retry_interval', 1.0))

        # Schedule parameters
        self.end_hour = int(config.get('schedule', {}).get('end_hour', 15))
        self.end_minute = int(config.get('schedule', {}).get('end_minute', 0))

        # SL Management parameters (strategy-level defaults, leg-level can override)
        # CRITICAL FIX: Remove hardcoded defaults to prevent cascade to legs
        # Only use what's explicitly in config file
        self.initial_sl_pct = config.get('strategy', {}).get('initial_sl_pct')
        self.lock_profit_pct = config.get('strategy', {}).get('lock_profit_pct')
        self.profit_lock_buffer = config.get('strategy', {}).get('profit_lock_buffer')
        self.auto_close_profit_pct = config.get('strategy', {}).get('auto_close_profit_pct')

        # Initialize components
        self.market_data = MarketDataManager(
            client,
            self._build_market_data_config(),
            websocket_client
        )
        self.order_manager = OrderManager(client, self._build_order_config())
        self.position_manager = PositionManager(self.order_manager, self._build_position_config())

        # Load legs configuration
        self.legs_config = self._load_legs_config()

        # ==== FETCH ONCE AT STARTUP (NO LOOPS) ====
        logger.info("ðŸ“… Fetching expiry date (one-time)...")
        self.expiry_date = self._fetch_expiry_once()
        logger.info(f"âœ“ Expiry: {self.expiry_date}")

        logger.info("ðŸ“Š Fetching highest high & lowest low (one-time)...")
        self.highest_high, self.lowest_low = self._fetch_high_low_once()
        logger.info(f"âœ“ High: {self.highest_high:.2f}, Low: {self.lowest_low:.2f}")

        # Initialize persistence
        self.persistence = None

        # Leg states
        self.leg_states: Dict[int, LegState] = {}
        self.state_lock = threading.Lock()

        logger.info(f"ðŸš€ Strategy initialized with {len(self.legs_config)} legs")

    def _fetch_expiry_once(self) -> str:
        """Fetch nearest expiry date from broker - called ONCE"""
        try:
            resp = self.client.expiry(
                symbol=self.underlying,
                exchange=self.option_exchange,
                instrumenttype=self.instrument_type
            )

            if resp.get('status') != 'success' or not resp.get('data'):
                raise RuntimeError(f"Invalid expiry response: {resp}")

            raw_expiry = resp['data'][0]  # e.g., '02-DEC-25'
            day, mon, year = raw_expiry.split('-')
            expiry_fmt = f"{day}{mon.upper()}{year[-2:]}"  # '02DEC25'

            return expiry_fmt
        except Exception as e:
            logger.error(f"âœ— Error fetching expiry: {e}")
            raise

    def _fetch_high_low_once(self) -> tuple[float, float]:
        """Fetch previous day high/low - called ONCE"""
        try:
            candles = self.market_data.get_previous_day_candles(self.tz)

            if candles is None or len(candles) == 0:
                raise RuntimeError("Could not fetch previous day candles")

            highest_high = float(candles['high'].max())
            lowest_low = float(candles['low'].min())

            return highest_high, lowest_low
        except Exception as e:
            logger.error(f"âœ— Error fetching high/low: {e}")
            raise

    def _build_market_data_config(self) -> Dict:
        """Build market data manager config"""
        return {
            'underlying': self.underlying,
            'underlying_exchange': self.config.get('strategy', {}).get('underlying_exchange', 'NSE_INDEX'),
            'option_exchange': self.option_exchange,
            'candle_interval': self.config.get('strategy', {}).get('candle_interval', '3m'),
            'lookback_candles': self.config.get('strategy', {}).get('lookback_candles_minutes', 3),
            'use_websocket': self.config.get('websocket', {}).get('enabled', False)
        }

    def _build_order_config(self) -> Dict:
        """Build order manager config"""
        return {
            'test_mode': self.config.get('strategy', {}).get('test_mode', False),
            'auto_place_orders': self.config.get('orders', {}).get('auto_place_orders', False),
            'option_exchange': self.option_exchange,
            'price_type': self.config.get('orders', {}).get('price_type', 'MARKET'),
            'product': self.config.get('orders', {}).get('product', 'NRML'),
            'instrument_type': self.instrument_type,
            'entry_action': self.config.get('orders', {}).get('entry_action', 'BUY'),
            'exit_action': self.config.get('orders', {}).get('exit_action', 'SELL'),
            'strategy_name': self.strategy_name  # Pass strategy name for order tagging
        }

    def _build_position_config(self) -> Dict:
        """Build position manager config"""
        return {
            'entry_action': self.config.get('orders', {}).get('entry_action', 'BUY'),
            'exit_action': self.config.get('orders', {}).get('exit_action', 'SELL')
        }

    def _load_legs_config(self) -> List[Dict]:
        """Load and validate legs configuration"""
        legs_raw = self.config.get('legs', [])

        if isinstance(legs_raw, list):
            legs = [leg for leg in legs_raw if leg.get('enabled', True)]
        else:
            logger.error("Invalid legs configuration format")
            legs = []

        if not legs:
            logger.warning("âš ï¸  No legs configured!")

        return legs

    def run(self):
        """Main strategy execution"""
        logger.info("\n" + "="*70)
        logger.info("ðŸš€ SEHWAG STRATEGY - CLEAN VERSION")
        logger.info("="*70)

        # Pre-flight checks
        if not is_market_open(self.tz):
            logger.warning("âš ï¸  Market is closed")
            return

        if not self.legs_config:
            logger.error("âŒ No legs configured")
            return

        # Initialize persistence
        try:
            self.persistence = SehwagPersistence(
                expiry_date=self.expiry_date,
                index_symbol=self.underlying,
                strike_diff=self.strike_diff,
                lot_size=self.lot_size
            )
        except Exception as e:
            logger.warning(f"⚠️  DB persistence disabled: {e}")

        # Initialize leg states with strategy defaults
        strategy_defaults = {
            'initial_sl_pct': self.initial_sl_pct,
            'lock_profit_pct': self.lock_profit_pct,
            'profit_lock_buffer': self.profit_lock_buffer,
            'auto_close_profit_pct': self.auto_close_profit_pct,
            'lot_multiplier': self.lot_multiplier
        }
        for i, leg_config in enumerate(self.legs_config, 1):
            self.leg_states[i] = LegState(i, leg_config, self.lot_size, strategy_defaults)

        # Start leg threads
        threads = []
        for leg_num, leg_state in self.leg_states.items():
            thread = threading.Thread(
                target=self._run_leg_thread,
                args=(leg_state,),
                name=f"Leg-{leg_num}-Thread",
                daemon=True
            )
            threads.append(thread)
            thread.start()
            logger.info(f"ðŸ§µ Started thread for {leg_state.name}")

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        logger.info("\nâœ… All legs completed")

        # Print summary
        self._print_summary()

        # Close persistence
        if self.persistence:
            total_pnl = sum(
                leg.calculate_pnl(leg.exit_price)[0]
                for leg in self.leg_states.values()
                if leg.exit_price
            )
            self.persistence.close_session(total_pnl, 0.0)

    def _wait_for_distance_stabilization(
        self,
        breakout_direction: str,
        breakout_distance: float,
        leg_state: LegState,
        leg_logger
    ) -> tuple[str, float]:
        """
        Check if breakout distance exceeds threshold and delay entry if needed.

        Simple behavior: If distance > threshold, delay entry by configured seconds.
        No re-checking - just delays the entry time once and proceeds.

        Args:
            breakout_direction: Breakout direction ("CE" or "PE")
            breakout_distance: Distance from breakout level
            leg_state: Leg state object with config
            leg_logger: Logger instance

        Returns:
            Tuple of (direction, distance) - always proceeds with entry after delay
        """
        diff_threshold = leg_state.config.get('breakout_diff_threshold', self.breakout_diff_threshold)
        diff_delay = leg_state.config.get('breakout_diff_delay_seconds', self.breakout_diff_delay_seconds)

        # If threshold not configured or distance already below threshold, proceed immediately
        if diff_threshold is None or breakout_distance <= diff_threshold:
            return (breakout_direction, breakout_distance)

        # Distance exceeds threshold - delay entry
        leg_logger.warning("=" * 80)
        leg_logger.warning(f"⚠️  BREAKOUT DISTANCE THRESHOLD EXCEEDED")
        leg_logger.warning(f"   Distance: {breakout_distance:.2f} points > Threshold: {diff_threshold:.2f} points")
        leg_logger.warning(f"   Delaying entry by {diff_delay} seconds to allow market stabilization")
        leg_logger.warning(f"   Original entry time will be shifted by {diff_delay}s")
        leg_logger.warning("=" * 80)

        # Simple delay - no re-checking
        delay_start = time.time()
        while (time.time() - delay_start) < diff_delay:
            remaining = diff_delay - (time.time() - delay_start)

            if remaining > 60:
                # Log every 30 seconds for long delays
                if int(remaining) % 30 == 0:
                    leg_logger.info(f"⏳ Entry delayed: {int(remaining)}s remaining...")
                time.sleep(30)
            elif remaining > 0:
                time.sleep(min(remaining, 5))
            else:
                break

        leg_logger.info("=" * 80)
        leg_logger.info(f"✅ DELAY COMPLETED ({diff_delay}s)")
        leg_logger.info(f"   Proceeding with entry at new delayed time")
        leg_logger.info("=" * 80)

        # Return original values - no re-check, always proceed
        return (breakout_direction, breakout_distance)

    def _run_leg_thread(self, leg_state: LegState):
        """
        Run a single leg in its own thread

        Flow:
        1. Wait for entry time
        2. Check breakout condition
        3. Wait for distance stabilization (if needed)
        4. Enter position if breakout valid
        5. Monitor position via WebSocket
        6. Exit on SL/target/time
        """
        leg_logger = get_leg_logger(leg_state.leg_num, leg_state.name)

        try:
            # Log immediately when leg thread starts
            current_time = datetime.now(self.tz).strftime('%H:%M:%S')
            leg_logger.info(f"🚀 Leg thread started at {current_time}")

            # Parse entry time
            entry_time_str = leg_state.config.get('entry_time', '09:15:10')
            hour, minute, second = map(int, entry_time_str.split(':'))
            entry_time = datetime.now(self.tz).replace(
                hour=hour, minute=minute, second=second, microsecond=0
            )
            leg_state.entry_time = entry_time

            # Parse exit time
            exit_time_str = leg_state.config.get('exit_time')
            if exit_time_str:
                hour, minute, second = map(int, exit_time_str.split(':'))
                exit_time = datetime.now(self.tz).replace(
                    hour=hour, minute=minute, second=second, microsecond=0
                )
                leg_state.exit_time = exit_time

            leg_logger.info(f"Entry scheduled at {entry_time.strftime('%H:%M:%S')}")

            # Print leg strategy summary
            self._print_leg_summary(leg_state, leg_logger)

            # Wait for entry time
            self._wait_for_time(entry_time, leg_logger)

            # Check if exit time has already passed
            now = datetime.now(self.tz)

            # Handle "strategy end time" (when exit_time is None)
            if leg_state.exit_time is None:
                strategy_end = now.replace(hour=self.end_hour, minute=self.end_minute, second=0, microsecond=0)
                if now >= strategy_end:
                    leg_logger.warning(f"⚠️  Strategy end time ({self.end_hour:02d}:{self.end_minute:02d}:00) has already passed!")
                    leg_logger.warning("Skipping entry to avoid immediate exit")
                    return
            # Handle specific exit time
            elif now >= leg_state.exit_time:
                leg_logger.warning(f"⚠️  Exit time ({leg_state.exit_time.strftime('%H:%M:%S')}) has already passed!")
                leg_logger.warning("Skipping entry to avoid immediate exit")
                return

            # Check if market is open
            if not is_market_open():
                leg_logger.warning("⚠️  Market is closed!")
                leg_logger.warning("Skipping entry - strategy should only run during market hours (9:15 AM - 3:30 PM)")
                return

            # Check breakout condition
            leg_logger.info("Checking breakout condition...")
            breakout_result = self._check_breakout_condition(leg_logger)
            breakout_direction, breakout_distance = breakout_result

            if not breakout_direction:
                leg_logger.warning("No breakout detected - skipping entry")
                return

            leg_logger.info(f"✓ Breakout detected: {breakout_direction} (Distance: {breakout_distance:.2f} points)")
            leg_state.entry_direction = breakout_direction

            # Check and delay entry if distance threshold exceeded
            breakout_direction, breakout_distance = self._wait_for_distance_stabilization(
                breakout_direction, breakout_distance, leg_state, leg_logger
            )
            leg_state.entry_direction = breakout_direction

            # Calculate the option symbol for this leg based on breakout direction
            strike_type = leg_state.config.get('strike_type', 'ATM')
            strike = self._calculate_strike_from_type(breakout_direction, strike_type)
            option_symbol = f"{self.underlying}{self.expiry_date}{strike}{breakout_direction}"

            leg_logger.info(f"Option to monitor: {option_symbol} (Strike: {strike_type})")

            # Get wait_trade parameters (leg-level has priority over strategy-level)
            wait_threshold = leg_state.config.get('wait_trade_threshold_pct', self.wait_trade_threshold_pct)
            wait_timeout = leg_state.config.get('wait_trade_timeout_seconds', self.wait_trade_timeout_seconds)

            # Get reset parameters from leg config
            reset_enabled = leg_state.config.get('wait_trade_reset_enabled', False)
            reset_drop_pct = leg_state.config.get('wait_trade_reset_drop_pct')

            # Treat string 'null' as None
            if isinstance(reset_drop_pct, str) and reset_drop_pct.lower() == 'null':
                reset_drop_pct = None

            leg_logger.info(f"Wait & Trade: {wait_threshold}% move, {wait_timeout}s timeout")
            if reset_enabled and reset_drop_pct:
                leg_logger.info(f"Reset Feature: Enabled (triggers on {reset_drop_pct}% drop)")

            # Wait for OPTION price movement confirmation (not NIFTY spot)
            if not self._wait_for_trade_confirmation(option_symbol, wait_threshold, leg_logger, wait_timeout,
                                                     reset_enabled, reset_drop_pct):
                leg_logger.warning("Wait & Trade confirmation failed - skipping entry")
                return

            leg_logger.info("✓ Wait & Trade confirmed")

            # Check if exit time has already passed before entering
            if leg_state.exit_time:
                current_time = datetime.now(self.tz)
                if current_time >= leg_state.exit_time:
                    leg_logger.warning(f"⚠️ Exit time ({leg_state.exit_time.strftime('%H:%M:%S')}) has already passed!")
                    leg_logger.warning(f"   Current time: {current_time.strftime('%H:%M:%S')}")
                    leg_logger.warning(f"   Skipping entry to avoid immediate exit")
                    return

            # Store the symbol in leg_state for entry
            leg_state.symbol = option_symbol

            # Enter position
            success = self._enter_leg_position(leg_state, leg_logger)
            if not success:
                leg_logger.error("Entry failed")
                return

            # Persist entry to DB
            if self.persistence:
                self.persistence.record_leg_entry(
                    leg_state.leg_num,
                    leg_state.name,
                    leg_state.symbol,
                    leg_state.entry_price,
                    leg_state.quantity,
                    leg_state.current_sl
                )

            # Monitor position
            self._monitor_leg_position(leg_state, leg_logger)

        except Exception as e:
            leg_logger.error(f"Thread error: {e}")
            # Log full traceback BEFORE closing logger
            import traceback
            leg_logger.error(f"Full traceback:\n{traceback.format_exc()}")
        finally:
            # Close the leg logger when thread completes
            close_leg_logger(leg_state.leg_num)

    def _print_leg_summary(self, leg_state: LegState, leg_logger):
        """Print leg configuration summary"""
        def get_leg_param(key, default=None):
            """Get parameter from leg config only (for display purposes)"""
            val = leg_state.config.get(key, default)
            # Treat string 'null' as None
            if isinstance(val, str) and val.lower() == 'null':
                return None
            return val

        def get_effective_param(key, default=None):
            """Get effective parameter with fallback to strategy defaults (for Wait & Trade display)"""
            return leg_state.config.get(key, leg_state.strategy_defaults.get(key, default))

        leg_logger.info("=" * 80)
        leg_logger.info(f"📋 LEG STRATEGY SUMMARY: {leg_state.name}")
        leg_logger.info("=" * 80)
        leg_logger.info(f"Entry Time:     {leg_state.config.get('entry_time', 'N/A')}")
        leg_logger.info(f"Exit Time:      {leg_state.config.get('exit_time', 'Strategy end time')}")
        leg_logger.info(f"Strike Type:    {leg_state.config.get('strike_type', 'ATM')}")

        # Show lot (leg-level) and lot_multiplier (strategy-level)
        lot = leg_state.config.get('lot', 1)
        lot_multiplier = leg_state.strategy_defaults.get('lot_multiplier', 1)
        leg_logger.info(f"Lot: {lot} | Multiplier: {lot_multiplier}x | Quantity: {leg_state.quantity}")
        leg_logger.info("")
        leg_logger.info("STOP LOSS MANAGEMENT:")
        leg_logger.info(f"  Initial SL:           {leg_state.initial_sl_pct}% below entry")

        sl_trail_trigger = get_leg_param('sl_trail_trigger_pct')
        sl_trail_move = get_leg_param('sl_trail_move_pct')
        if sl_trail_trigger and sl_trail_move:
            leg_logger.info(f"  Trailing SL:          Every {sl_trail_trigger}% profit gain → Move SL up by {sl_trail_move}%")
        else:
            leg_logger.info(f"  Trailing SL:          Disabled (Fixed SL)")

        leg_logger.info("")
        leg_logger.info("PROFIT MANAGEMENT:")

        # Check which mode is configured (use leg-specific params only)
        first_lock = get_leg_param('first_lock_pct')
        trail_trigger = get_leg_param('trail_trigger_pct')
        trail_move = get_leg_param('trail_move_pct')
        lock_profit = get_leg_param('lock_profit_pct')
        lock_trigger = get_leg_param('lock_trigger_pct')
        profit_step = get_leg_param('profit_lock_step')
        profit_threshold = get_leg_param('profit_step_threshold')

        if lock_trigger and first_lock and trail_trigger and trail_move:
            # Mode 2: Two-stage profit lock (trigger → lock → trail)
            leg_logger.info(f"  Mode:                 Two-Stage Profit Lock")
            leg_logger.info(f"  Lock Trigger:         {lock_trigger}% profit → activate lock")
            leg_logger.info(f"  First Lock:           Exit at {first_lock}% profit (when triggered)")
            leg_logger.info(f"  Then Trail:           Every {trail_trigger}% profit gain → Trail exit by {trail_move}%")
        elif first_lock and trail_trigger and trail_move:
            # Mode 2b: Lock immediately then trail
            leg_logger.info(f"  Mode:                 Lock Once Then Trail")
            leg_logger.info(f"  First Lock:           Exit at {first_lock}% profit (locks immediately)")
            leg_logger.info(f"  Then Trail:           Every {trail_trigger}% profit gain → Trail exit by {trail_move}%")
        elif profit_step and profit_threshold and lock_profit:
            # Mode 3: Progressive escalating
            leg_logger.info(f"  Mode:                 Progressive Escalating Lock")
            leg_logger.info(f"  Initial Target:       {lock_profit}%")
            leg_logger.info(f"  Escalation:           +{profit_step}% target every {profit_threshold}% profit gain")
        elif lock_profit:
            # Mode 1: Simple lock
            leg_logger.info(f"  Mode:                 Simple Profit Lock")
            leg_logger.info(f"  Target:               Exit at {lock_profit}% profit")
        else:
            leg_logger.info(f"  Mode:                 None configured (only SL)")

        auto_close = get_leg_param('auto_close_profit_pct')
        if auto_close:
            leg_logger.info(f"  Auto Close:           {auto_close}% (overrides all other exits)")

        leg_logger.info("")
        leg_logger.info("ENTRY CONFIRMATION:")
        wait_threshold = get_effective_param('wait_trade_threshold_pct', self.wait_trade_threshold_pct)
        wait_timeout = get_effective_param('wait_trade_timeout_seconds', self.wait_trade_timeout_seconds)
        leg_logger.info(f"  Wait & Trade:         {wait_threshold}% move required, {wait_timeout}s timeout")

        # Show reset feature status
        reset_enabled = get_leg_param('wait_trade_reset_enabled')
        reset_drop_pct = get_leg_param('wait_trade_reset_drop_pct')
        if reset_enabled and reset_drop_pct:
            leg_logger.info(f"  Reset Feature:        Enabled (triggers on {reset_drop_pct}% drop from peak)")
        else:
            leg_logger.info(f"  Reset Feature:        Disabled")

        # Show breakout distance delay status
        diff_threshold = get_effective_param('breakout_diff_threshold', self.breakout_diff_threshold)
        diff_delay = get_effective_param('breakout_diff_delay_seconds', self.breakout_diff_delay_seconds)
        if diff_threshold is not None:
            leg_logger.info(f"  Distance Delay:       Enabled ({diff_threshold} points → {diff_delay}s delay)")
        else:
            leg_logger.info(f"  Distance Delay:       Disabled")

        leg_logger.info("=" * 80)

        # ADD CONFIG VALIDATION - Show effective values after resolution
        leg_logger.info("")
        leg_logger.info("=" * 80)
        leg_logger.info("🔍 EFFECTIVE CONFIG (What will ACTUALLY be used):")
        leg_logger.info("=" * 80)

        # Helper to check effective value with proper null handling
        def show_effective_value(param_name, display_name):
            """Show effective value and its source"""
            leg_val = leg_state.config.get(param_name)
            strategy_val = leg_state.strategy_defaults.get(param_name) if leg_state.strategy_defaults else None

            # Check if explicitly disabled in leg config
            is_leg_null = leg_val is None or (isinstance(leg_val, str) and leg_val.lower() in ('null', 'none', ''))
            is_strategy_null = strategy_val is None or (isinstance(strategy_val, str) and strategy_val.lower() in ('null', 'none', ''))

            if param_name in leg_state.config and not is_leg_null:
                leg_logger.info(f"  {display_name:30s} = {leg_val:>8} (LEG config)")
            elif param_name in leg_state.config and is_leg_null:
                leg_logger.info(f"  {display_name:30s} = {'None':>8} (DISABLED in leg config)")
            elif strategy_val is not None and not is_strategy_null:
                leg_logger.info(f"  {display_name:30s} = {strategy_val:>8} (strategy default)")
            else:
                leg_logger.info(f"  {display_name:30s} = {'None':>8} (not configured)")

        # Show all relevant parameters
        show_effective_value('first_lock_pct', 'first_lock_pct')
        show_effective_value('trail_trigger_pct', 'trail_trigger_pct')
        show_effective_value('trail_move_pct', 'trail_move_pct')
        show_effective_value('lock_trigger_pct', 'lock_trigger_pct')
        show_effective_value('lock_profit_pct', 'lock_profit_pct')
        show_effective_value('profit_lock_step', 'profit_lock_step')
        show_effective_value('profit_step_threshold', 'profit_step_threshold')
        show_effective_value('auto_close_profit_pct', 'auto_close_profit_pct')
        show_effective_value('sl_trail_trigger_pct', 'sl_trail_trigger_pct')
        show_effective_value('sl_trail_move_pct', 'sl_trail_move_pct')

        leg_logger.info("=" * 80)
        leg_logger.info("")
        leg_logger.info("⚠️  CRITICAL: Params with 'DISABLED in leg config' will NOT use strategy defaults!")
        leg_logger.info("=" * 80)

    def _wait_for_time(self, target_time: datetime, leg_logger):
        """Wait until target time"""
        now = datetime.now(self.tz)
        remaining = (target_time - now).total_seconds()

        # If already past target time, return immediately
        if remaining <= 0:
            leg_logger.info(f"✓ Time reached: {now.strftime('%H:%M:%S')}")
            return

        # Log initial wait message
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        leg_logger.info(f"⏳ Waiting {minutes}m {seconds}s until entry time {target_time.strftime('%H:%M:%S')}")

        # Wait with periodic logging (every 30 seconds if waiting > 1 minute)
        last_log_time = now
        while datetime.now(self.tz) < target_time:
            remaining = (target_time - datetime.now(self.tz)).total_seconds()

            # Log every 30 seconds if waiting more than 1 minute
            if remaining > 60 and (datetime.now(self.tz) - last_log_time).total_seconds() >= 30:
                minutes = int(remaining // 60)
                seconds = int(remaining % 60)
                leg_logger.info(f"⏳ Still waiting... {minutes}m {seconds}s remaining")
                last_log_time = datetime.now(self.tz)

            if remaining > 60:
                time.sleep(30)
            elif remaining > 0:
                time.sleep(min(remaining, 1))
            else:
                break

        leg_logger.info(f"✓ Time reached: {datetime.now(self.tz).strftime('%H:%M:%S')}")

    def _check_breakout_condition(self, leg_logger) -> tuple[Optional[str], float]:
        """
        Check if NIFTY spot has broken above highest_high or below lowest_low

        Returns:
            Tuple of (direction, distance) where:
            - direction: "CE" if breakout above, "PE" if breakout below, None otherwise
            - distance: Points difference from breakout level
            Returns (None, 0.0) if no breakout
        """
        current_spot = self.market_data.get_underlying_price()

        if not current_spot:
            leg_logger.error("Could not fetch spot price")
            return (None, 0.0)

        leg_logger.info(f"Spot: ₹{current_spot:.2f}, High: ₹{self.highest_high:.2f}, Low: ₹{self.lowest_low:.2f}")

        if current_spot > self.highest_high:
            distance = current_spot - self.highest_high
            leg_logger.info(f"✓ Breakout ABOVE highest high ({current_spot:.2f} > {self.highest_high:.2f}), Distance: {distance:.2f} points")
            return ("CE", distance)
        elif current_spot < self.lowest_low:
            distance = self.lowest_low - current_spot
            leg_logger.info(f"✓ Breakout BELOW lowest low ({current_spot:.2f} < {self.lowest_low:.2f}), Distance: {distance:.2f} points")
            return ("PE", distance)
        else:
            leg_logger.info(f"No breakout (spot within range)")
            return (None, 0.0)

    def _wait_for_trade_confirmation(self, option_symbol: str, threshold_pct: float,
                                     leg_logger, timeout: int, reset_enabled: bool = False,
                                     reset_drop_pct: Optional[float] = None,
                                     debug_return_reset_count: bool = False) -> bool:
        """
        Wait for OPTION price to move threshold_pct from initial price

        Uses WebSocket for real-time monitoring with REST API fallback
        Supports automatic reset if price drops during wait period

        Args:
            option_symbol: Option symbol to monitor (e.g., NIFTY25DEC24500CE)
            threshold_pct: Percentage move required (e.g., 3.0 for 3%)
            leg_logger: Logger instance
            timeout: Maximum wait time in seconds (from config)
            reset_enabled: Enable automatic reset on price drop
            reset_drop_pct: Drop % from peak to trigger reset

        Returns:
            True if threshold reached, False if timeout
        """
        # Get initial option price
        initial_quote = self.market_data.get_quote(option_symbol, self.option_exchange)
        if not initial_quote or 'ltp' not in initial_quote:
            leg_logger.error(f"Could not fetch initial price for {option_symbol}")
            return False

        reference_price = float(initial_quote['ltp'])
        target_price = reference_price * (1 + threshold_pct / 100)
        price_difference = target_price - reference_price

        # Initialize peak tracking for reset logic
        # highest_since_reference tracks the highest option price observed since the
        # most recent reference price (resets when we perform a wait&trade reset).
        highest_since_reference = reference_price
        reset_count = 0

        # Get current NIFTY spot for context
        current_spot = self.market_data.get_underlying_price()
        spot_context = f" | NIFTY Spot: ₹{current_spot:.2f}" if current_spot else ""

        # Determine breakout type and level
        is_ce = 'CE' in option_symbol
        breakout_type = 'High' if is_ce else 'Low'
        breakout_level = self.highest_high if is_ce else self.lowest_low

        leg_logger.info("=" * 80)
        leg_logger.info(f"📊 WAIT & TRADE MONITORING: {option_symbol}")
        leg_logger.info(f"   Starting Price:  ₹{reference_price:.2f}")
        leg_logger.info(f"   Target Price:    ₹{target_price:.2f}  (need +₹{price_difference:.2f} / +{threshold_pct}%)")
        leg_logger.info(f"   Breakout Level:  {breakout_type} = ₹{breakout_level:.2f}{spot_context}")
        leg_logger.info(f"   Timeout:         {timeout}s ({timeout//60}m {timeout%60}s)")
        if reset_enabled and reset_drop_pct:
            leg_logger.info(f"   Reset Feature:   Enabled (triggers on {reset_drop_pct}% drop from peak)")
        leg_logger.info("=" * 80)

        # Subscribe to WebSocket if available
        if self.websocket_client:
            try:
                self.websocket_client.subscribe_ltp_sync(option_symbol, self.option_exchange)
                leg_logger.info("✓ WebSocket subscribed for real-time monitoring")
            except Exception as e:
                leg_logger.warning(f"WebSocket subscription failed: {e}, will use REST API")

        start_time = time.time()
        last_log_time = start_time
        log_interval = 1  # Log every 10 seconds

        while True:
            # Get current option price (WebSocket first, then REST API fallback)
            current_price = None

            # Try WebSocket first
            if self.websocket_client:
                current_price = self.market_data._get_price_from_websocket(option_symbol)

            # Fallback to REST API
            if not current_price:
                quote = self.market_data.get_quote(option_symbol, self.option_exchange)
                if quote and 'ltp' in quote:
                    current_price = float(quote['ltp'])

            if current_price:
                # Update highest seen since the last reference (re-arm for resets)
                if current_price > highest_since_reference:
                    highest_since_reference = current_price

                # Check if threshold reached (option prices always move upward for positive movement)
                move_pct = ((current_price - reference_price) / reference_price) * 100

                if current_price >= target_price:
                    price_gain = current_price - reference_price
                    leg_logger.info("=" * 80)
                    leg_logger.info(f"✅ THRESHOLD REACHED!")
                    leg_logger.info(f"   {option_symbol}: ₹{reference_price:.2f} → ₹{current_price:.2f}")
                    leg_logger.info(f"   Gain: +₹{price_gain:.2f} ({move_pct:+.2f}%) | Target was {threshold_pct}%")
                    if reset_count > 0:
                        leg_logger.info(f"   Resets triggered: {reset_count}")
                    leg_logger.info("=" * 80)
                    return (True, reset_count) if debug_return_reset_count else True

                # Check for reset condition using highest_since_reference (robust re-arm)
                if reset_enabled and reset_drop_pct:
                    drop_from_peak = ((highest_since_reference - current_price) / highest_since_reference) * 100

                    if drop_from_peak >= reset_drop_pct:
                        reset_count += 1
                        old_reference = reference_price
                        old_target = target_price
                        old_peak = highest_since_reference

                        # Reset to current price and re-arm highest_since_reference
                        reference_price = current_price
                        target_price = reference_price * (1 + threshold_pct / 100)
                        highest_since_reference = current_price

                        leg_logger.warning("=" * 80)
                        leg_logger.warning(f"🔄 WAIT & TRADE RESET #{reset_count}")
                        leg_logger.warning(f"   Price dropped {drop_from_peak:.2f}% from peak ₹{old_peak:.2f}")
                        leg_logger.warning(f"   Old Reference: ₹{old_reference:.2f} → New Reference: ₹{reference_price:.2f}")
                        leg_logger.warning(f"   Old Target:    ₹{old_target:.2f} → New Target:    ₹{target_price:.2f}")
                        leg_logger.warning(f"   Waiting for {threshold_pct}% move from new reference...")
                        leg_logger.warning("=" * 80)

                # Periodic logging with detailed information
                elapsed = time.time() - start_time
                if (time.time() - last_log_time) >= log_interval:
                    progress = (move_pct / threshold_pct) * 100 if move_pct > 0 else 0
                    remaining = timeout - elapsed

                    # Get current NIFTY spot for context
                    current_spot = self.market_data.get_underlying_price()
                    spot_info = f", NIFTY: ₹{current_spot:.2f}" if current_spot else ""

                    # Add reset info if applicable
                    reset_info = f", Resets: {reset_count}" if reset_count > 0 else ""

                    leg_logger.info(
                        f"⏳ Monitoring {option_symbol}: Current ₹{current_price:.2f} → Target ₹{target_price:.2f} "
                        f"(Need {move_pct:+.2f}% of {threshold_pct}%{spot_info}{reset_info}) | "
                        f"Progress: {min(progress, 100):.1f}% | Time left: {int(remaining)}s"
                    )
                    last_log_time = time.time()

            # Check timeout
            if (time.time() - start_time) > timeout:
                final_move = ((current_price - reference_price) / reference_price * 100) if current_price else 0
                leg_logger.warning("=" * 80)
                leg_logger.warning(f"⏱️ WAIT & TRADE TIMEOUT")
                leg_logger.warning(f"   {option_symbol}: ₹{reference_price:.2f} → ₹{current_price:.2f}" if current_price else f"   No price data received")
                leg_logger.warning(f"   Moved: {final_move:+.2f}% (needed {threshold_pct}%) in {timeout}s")
                if reset_count > 0:
                    leg_logger.warning(f"   Resets triggered: {reset_count}")
                leg_logger.warning(f"   Entry SKIPPED - threshold not reached in time")
                leg_logger.warning("=" * 80)
                return (False, reset_count) if debug_return_reset_count else False

            time.sleep(self.wait_trade_check_interval)

    def _enter_leg_position(self, leg_state: LegState, leg_logger) -> bool:
        """Enter position for a leg"""
        try:
            # Symbol already calculated during wait_for_trade_confirmation
            if not leg_state.symbol:
                leg_logger.error("Symbol not set - this should not happen")
                return False

            leg_logger.info(f"Entering position: {leg_state.symbol}")

            # Get current quote for entry (LTP reference)
            quote = self.market_data.get_quote(leg_state.symbol, self.option_exchange)
            if not quote or 'ltp' not in quote:
                leg_logger.error("Could not fetch LTP for entry")
                return False

            # Store LTP as initial entry price (will be updated with actual fill price)
            ltp_price = float(quote['ltp'])
            leg_state.entry_price = ltp_price
            leg_logger.info(f"Entry price (LTP): ₹{ltp_price:.2f}")

            # Place entry order
            order_id = self.order_manager.place_order(
                symbol=leg_state.symbol,
                quantity=leg_state.quantity,
                action=self.order_manager.entry_action
            )

            if order_id:
                leg_state.entry_order_id = order_id
                leg_logger.info(f"✅ Entry order placed: {order_id}")

                # Fetch ACTUAL fill price from broker (for accurate P&L tracking)
                actual_fill_price = self.order_manager.get_fill_price(order_id, max_wait_seconds=5, custom_logger=leg_logger)

                if actual_fill_price and actual_fill_price > 0:
                    # Update entry price with actual broker fill price
                    price_diff = actual_fill_price - ltp_price
                    price_diff_pct = (price_diff / ltp_price) * 100

                    leg_state.entry_price = actual_fill_price

                    if abs(price_diff) > 0.01:  # Only log if difference is significant
                        if price_diff > 0:
                            leg_logger.warning(f"⚠️ Slippage: Filled @ ₹{actual_fill_price:.2f} vs LTP ₹{ltp_price:.2f} (+₹{price_diff:.2f} / +{price_diff_pct:.2f}%)")
                        else:
                            leg_logger.info(f"✅ Filled @ ₹{actual_fill_price:.2f} vs LTP ₹{ltp_price:.2f} (₹{price_diff:.2f} / {price_diff_pct:.2f}%)")
                    else:
                        leg_logger.info(f"✅ Filled @ ₹{actual_fill_price:.2f} (matches LTP)")
                else:
                    leg_logger.warning(f"⚠️ Could not fetch fill price from broker, using LTP: ₹{ltp_price:.2f}")
                    leg_logger.warning(f"   P&L calculations may be slightly inaccurate due to slippage")

                # Calculate SL based on ACTUAL entry price (ensure float)
                leg_state.current_sl = float(leg_state.entry_price * (1 - leg_state.initial_sl_pct / 100))
                leg_logger.info(f"Initial SL: â‚¹{leg_state.current_sl:.2f} (based on actual entry)")

                # Mark position as active
                leg_state.is_active = True
                leg_state.highest_price = float(leg_state.entry_price)  # Ensure float

                # Place SL order on broker (following Expiry Blast standard)
                sl_order_id = self.order_manager.place_sl_order(
                    symbol=leg_state.symbol,
                    quantity=leg_state.quantity,
                    stop_price=leg_state.current_sl,
                    strategy_name=f"{self.strategy_name}_{leg_state.name.replace(' ', '_')}"
                )

                if sl_order_id:
                    leg_state.sl_order_id = sl_order_id
                    leg_logger.info(f"✅ SL order placed on broker: {sl_order_id} @ ₹{leg_state.current_sl:.2f}")

                    # Log to database
                    if self.persistence:
                        try:
                            self.persistence.log_event(
                                "SL_ORDER_PLACED",
                                f"Leg {leg_state.leg_num} SL order placed on broker: {sl_order_id}",
                                metadata={
                                    'leg_num': leg_state.leg_num,
                                    'sl_order_id': sl_order_id,
                                    'sl_price': leg_state.current_sl,
                                    'symbol': leg_state.symbol,
                                    'quantity': leg_state.quantity
                                }
                            )
                        except Exception as log_error:
                            leg_logger.warning(f"⚠️ Could not log SL order to database: {log_error}")
                else:
                    leg_logger.warning(f"⚠️ SL order not placed on broker (test mode or config disabled)")

                # Note: WebSocket subscription already done during Wait & Trade phase
                # No need to subscribe again here to avoid duplicate subscription logs

                return True
            else:
                leg_logger.error("Order placement failed")
                return False

        except Exception as e:
            leg_logger.error(f"Entry error: {e}")
            return False

    def _calculate_strike_from_type(self, direction: str, strike_type: str) -> int:
        """
        Calculate strike based on strike type

        Args:
            direction: "CE" or "PE"
            strike_type: "ATM", "ITM1", "ITM2", "ITM3", "OTM1", "OTM2", etc.

        Returns:
            Strike price as integer
        """
        spot = self.market_data.get_underlying_price()
        if not spot:
            raise RuntimeError("Could not fetch spot price")

        # Parse strike type
        strike_type_upper = strike_type.upper()

        # For ATM, calculate rounded strike
        if strike_type_upper == "ATM":
            atm_strike = round(spot / self.strike_diff) * self.strike_diff
            return int(atm_strike)

        # For ITM/OTM, calculate directly from spot
        if strike_type_upper.startswith("ITM"):
            level = int(strike_type_upper.replace("ITM", ""))
            # ITM for CE means LOWER strike (below spot), ITM for PE means HIGHER strike (above spot)
            if direction == "CE":
                strike = round(spot / self.strike_diff) * self.strike_diff - (level * self.strike_diff)
            else:  # PE
                strike = round(spot / self.strike_diff) * self.strike_diff + (level * self.strike_diff)
        elif strike_type_upper.startswith("OTM"):
            level = int(strike_type_upper.replace("OTM", ""))
            # OTM for CE means HIGHER strike (above spot), OTM for PE means LOWER strike (below spot)
            if direction == "CE":
                strike = round(spot / self.strike_diff) * self.strike_diff + (level * self.strike_diff)
            else:  # PE
                strike = round(spot / self.strike_diff) * self.strike_diff - (level * self.strike_diff)
        else:
            raise ValueError(f"Invalid strike_type: {strike_type}. Use ATM, ITM1, ITM2, OTM1, OTM2, etc.")

        return int(strike)

    def _monitor_leg_position(self, leg_state: LegState, leg_logger):
        """
        Monitor position using WebSocket (fallback to REST API)

        Exit conditions:
        - SL breach
        - Profit target
        - Exit time reached
        """
        leg_logger.info("ðŸ“Š Monitoring position...")

        # Register WebSocket callback if available
        if self.websocket_client:
            def on_price_update(ltp):
                try:
                    # Ensure price is float (WebSocket may return string)
                    if ltp is None:
                        return
                    ltp_float = float(ltp)
                    self._handle_price_update(leg_state, ltp_float, leg_logger)
                except (TypeError, ValueError) as e:
                    leg_logger.error(f"Price conversion error: ltp={ltp} (type={type(ltp).__name__}), error={e}")
                except Exception as e:
                    leg_logger.error(f"Callback error: {e}", exc_info=True)

            self.websocket_client.on_price_update(leg_state.symbol, on_price_update)

        # Initialize staleness tracking
        if not hasattr(leg_state, '_last_price'):
            leg_state._last_price = None
            leg_state._last_price_time = time.time()
            leg_state._stale_price_count = 0
            leg_state._last_staleness_check = time.time()

        # Monitor loop
        while leg_state.is_active:
            try:
                # Get current price
                current_price = None
                use_fallback = False

                # Check if WebSocket is available and connected
                ws_available = self.websocket_client and hasattr(self.websocket_client, 'is_connected') and self.websocket_client.is_connected()

                if ws_available:
                    current_price = self.websocket_client.get_last_price(leg_state.symbol)

                    # Detect stale price (same price for too long) - time-based, not iteration-based
                    current_time = time.time()
                    if current_price and leg_state._last_price:
                        if float(current_price) == leg_state._last_price:
                            # Only increment counter once per second
                            if current_time - leg_state._last_staleness_check >= 1.0:
                                leg_state._stale_price_count += 1
                                leg_state._last_staleness_check = current_time

                                # Warn at 10 seconds
                                if leg_state._stale_price_count == 10:
                                    leg_logger.warning(f"⚠️ WebSocket price unchanged for 10s (₹{current_price:.2f}) - monitoring for staleness")
                                # If price unchanged for 30+ seconds, use REST API fallback
                                elif leg_state._stale_price_count >= 30:
                                    leg_logger.warning(f"⚠️ WebSocket price stale ({leg_state._stale_price_count}s), switching to REST API")
                                    use_fallback = True
                        else:
                            # Price changed - reset everything
                            leg_state._stale_price_count = 0
                            leg_state._last_price = float(current_price)
                            leg_state._last_staleness_check = current_time
                    elif current_price:
                        leg_state._last_price = float(current_price)
                        leg_state._stale_price_count = 0
                        leg_state._last_staleness_check = current_time
                else:
                    if self.websocket_client:
                        leg_logger.debug("WebSocket disconnected, using REST API")
                    use_fallback = True

                # Use REST API if no WebSocket, disconnected, or fallback needed
                if not ws_available or use_fallback or not current_price:
                    quote = self.market_data.get_quote(leg_state.symbol, self.option_exchange)
                    if quote:
                        current_price = quote.get('ltp')
                        # Reset staleness counter when REST API provides new price
                        if current_price and float(current_price) != leg_state._last_price:
                            leg_state._stale_price_count = 0
                            leg_state._last_price = float(current_price)

                if current_price:
                    # Ensure price is float (not string)
                    current_price = float(current_price)
                    self._handle_price_update(leg_state, current_price, leg_logger)
                else:
                    leg_logger.warning("⚠️ No price data available")

                # Check exit time
                if leg_state.exit_time and datetime.now(self.tz) >= leg_state.exit_time:
                    leg_logger.info("Exit time reached")
                    self._exit_leg_position(leg_state, current_price, "TIME_EXIT", leg_logger)
                    break

                # Small sleep to prevent CPU spinning
                # WebSocket provides real-time updates, so short sleep is sufficient
                time.sleep(self.monitor_check_interval if self.websocket_client else self.monitor_check_interval_no_ws)

            except Exception as e:
                leg_logger.error(f"Monitor error: {e}")
                time.sleep(self.error_retry_interval)

        leg_logger.info("âœ“ Monitoring ended")

    def _handle_price_update(self, leg_state: LegState, current_price: float, leg_logger):
        """Handle price update and check exit conditions"""
        if not leg_state.is_active or not current_price:
            return

        # Check if exit is in progress (prevents race condition)
        if hasattr(leg_state, '_exiting') and leg_state._exiting:
            return

        try:
            # Ensure price is float (safety check)
            current_price = float(current_price)

            # Ensure leg_state values are also float
            if not isinstance(leg_state.highest_price, (int, float)):
                leg_logger.error(f"Type error: highest_price is {type(leg_state.highest_price).__name__}: {leg_state.highest_price!r}")
                leg_state.highest_price = float(leg_state.highest_price)

            if not isinstance(leg_state.current_sl, (int, float)):
                leg_logger.error(f"Type error: current_sl is {type(leg_state.current_sl).__name__}: {leg_state.current_sl!r}")
                leg_state.current_sl = float(leg_state.current_sl)
        except (TypeError, ValueError) as e:
            leg_logger.error(f"Price type conversion error in handle_price_update: current_price={current_price} (type={type(current_price).__name__}), error={e}")
            return

        # Update highest price
        if current_price > leg_state.highest_price:
            leg_state.highest_price = current_price

        # Calculate P&L
        pnl, pnl_pct = leg_state.calculate_pnl(current_price)

        # Check SL breach
        if current_price <= leg_state.current_sl:
            leg_logger.warning(f"⚠️  SL breached at ₹{current_price:.2f}")

            # CRITICAL FIX: Check if SL order on broker already executed
            # If SL order executed, position is already closed - don't place duplicate sell order!
            if leg_state.sl_order_id:
                # Check if SL order still exists (pending) or already executed/filled
                try:
                    order_status = self.order_manager.check_order_status(leg_state.sl_order_id)

                    if order_status and order_status.lower() in ['complete', 'filled', 'executed']:
                        # SL order already executed on broker - position is closed
                        leg_logger.info(f"✅ SL order {leg_state.sl_order_id} already executed on broker")
                        leg_logger.info(f"   Position closed by broker's SL, no manual exit needed")

                        # Mark position as closed without placing another sell order
                        with self.state_lock:
                            leg_state._exiting = True
                            leg_state.is_active = False
                            leg_state.sl_order_id = None
                            leg_state.exit_price = current_price
                            leg_state.exit_reason = "SL_EXECUTED_ON_BROKER"

                        # Log to database
                        if self.persistence:
                            pnl, pnl_pct = leg_state.calculate_pnl(current_price)
                            self.persistence.record_leg_exit(
                                leg_state.leg_num,
                                current_price,
                                "SL_EXECUTED_ON_BROKER",
                                realized_pnl=pnl,
                                pnl_percentage=pnl_pct
                            )

                        leg_logger.info(f"✅ Position marked as closed (SL executed on broker)")
                        return

                except Exception as e:
                    leg_logger.warning(f"⚠️ Could not check SL order status: {e}")
                    leg_logger.warning(f"   Will attempt manual exit (may fail if already closed)")

            # SL order not executed yet or doesn't exist - proceed with manual exit
            self._exit_leg_position(leg_state, current_price, "SL_BREACH", leg_logger)
            return

        # Unified management logic - leg-level config takes precedence over strategy defaults
        self._manage_position_unified(leg_state, current_price, pnl_pct, leg_logger)

        # Log periodic updates - every second, but prevent duplicates
        current_second = int(time.time())
        if not hasattr(leg_state, 'last_log_second'):
            leg_state.last_log_second = 0

        if current_second > leg_state.last_log_second:
            leg_state.last_log_second = current_second

            # Build detailed monitoring log
            log_parts = []
            log_parts.append(f"LTP: ₹{current_price:.2f}")
            log_parts.append(f"Entry: ₹{leg_state.entry_price:.2f}")
            log_parts.append(f"P&L: {pnl_pct:+.2f}%")
            log_parts.append(f"SL: ₹{leg_state.current_sl:.2f}")

            # Add profit lock status if enabled
            if leg_state.first_lock_achieved and leg_state.profit_exit_target is not None:
                exit_target_price = leg_state.entry_price * (1 + leg_state.profit_exit_target / 100)
                log_parts.append(f"Lock: ✓ (Exit@₹{exit_target_price:.2f}/{leg_state.profit_exit_target:+.1f}%)")

                # Show next trail threshold
                def get_param(key, default=None):
                    return leg_state.config.get(key, leg_state.strategy_defaults.get(key, default))
                trail_trigger = get_param('trail_trigger_pct')
                if trail_trigger:
                    next_trail_at = leg_state.last_trail_level + float(trail_trigger)
                    log_parts.append(f"Next@{next_trail_at:.1f}%")
            else:
                # Show when lock will trigger
                def get_param(key, default=None):
                    return leg_state.config.get(key, leg_state.strategy_defaults.get(key, default))
                first_lock = get_param('first_lock_pct')
                lock_trigger = get_param('lock_trigger_pct', first_lock)
                if lock_trigger is not None and lock_trigger not in ('null', 'none', ''):
                    try:
                        lock_trigger_float = float(lock_trigger)
                        log_parts.append(f"Lock: ✗ (Trigger@{lock_trigger_float:.1f}%)")
                    except (ValueError, TypeError):
                        pass

            # Add highest price reached (always show when position is active)
            if leg_state.highest_price:
                log_parts.append(f"Peak: ₹{leg_state.highest_price:.2f}")

            leg_logger.info(" | ".join(log_parts))

    def _manage_position_unified(self, leg_state: LegState, current_price: float, pnl_pct: float, leg_logger):
        """
        Unified flexible position management

        Three modes:
        1. Simple profit lock (lock_profit_pct only)
        2. Lock once then trail (first_lock_pct + trail_trigger_pct + trail_move_pct)
        3. Progressive escalating lock (lock_profit_pct + profit_lock_step + profit_step_threshold)
        """
        # Helper to safely convert config values to float
        def safe_float(value):
            """Convert config value to float, handling 'null' strings from YAML"""
            if value is None:
                return None
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                # Handle YAML 'null' string
                if value.lower() in ('null', 'none', ''):
                    return None
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return None
            return None

        # FIXED: Get config with proper null handling to prevent cascade bugs
        def get_param(key, default=None, allow_strategy_fallback=True):
            """
            Get parameter value with proper None/null handling

            Args:
                key: Parameter key to lookup
                default: Default value if not found
                allow_strategy_fallback: If False, explicit None in leg config blocks strategy fallback

            Returns:
                Parameter value or None

            Behavior:
                - If key exists in leg config with non-null value → use it
                - If key exists in leg config with null/None → return None (do NOT fall back)
                - If key NOT in leg config and allow_strategy_fallback → check strategy defaults
                - Otherwise → return default
            """
            # Check if key exists in leg config
            if key in leg_state.config:
                value = leg_state.config[key]
                # Check if explicitly disabled (None or "null" string from YAML)
                if value is None or (isinstance(value, str) and value.lower() in ('null', 'none', '')):
                    # Explicitly disabled - do not fall back to strategy defaults
                    return None
                # Has a real value
                return value

            # Not in leg config - check strategy defaults if allowed
            if allow_strategy_fallback and key in leg_state.strategy_defaults:
                value = leg_state.strategy_defaults[key]
                # Apply same null check for strategy defaults
                if value is None or (isinstance(value, str) and value.lower() in ('null', 'none', '')):
                    return None
                return value

            # Not found anywhere
            return default

        # ===== 1. TRAILING STOP LOSS MANAGEMENT =====
        # Trail SL when profit moves X%, then move SL by Y%
        sl_trail_trigger = safe_float(get_param('sl_trail_trigger_pct'))
        sl_trail_move = safe_float(get_param('sl_trail_move_pct'))

        if sl_trail_trigger and sl_trail_move and pnl_pct > 0:
            # Check if profit has increased by trigger amount since last trail
            profit_increase = pnl_pct - leg_state.last_sl_trail_level

            if profit_increase >= sl_trail_trigger:
                # Calculate how many trigger intervals we've crossed
                intervals_crossed = int(profit_increase / sl_trail_trigger)

                # Move SL up by move_pct * intervals
                sl_increase_pct = sl_trail_move * intervals_crossed
                new_sl_price = leg_state.current_sl * (1 + sl_increase_pct / 100)

                if new_sl_price > leg_state.current_sl:
                    old_sl = leg_state.current_sl
                    leg_state.current_sl = new_sl_price
                    leg_state.last_sl_trail_level = pnl_pct  # Update last trail level

                    leg_logger.info(f"📈 Trailing SL: ₹{old_sl:.2f} → ₹{new_sl_price:.2f} "
                                  f"(Profit: {pnl_pct:.2f}% → SL moved up {sl_increase_pct:.2f}%)")

                    # Modify SL order on broker (following Expiry Blast standard)
                    if leg_state.sl_order_id:
                        success = self.order_manager.modify_sl_order(
                            order_id=leg_state.sl_order_id,
                            symbol=leg_state.symbol,
                            quantity=leg_state.quantity,
                            new_stop_price=new_sl_price,
                            strategy_name=f"{self.strategy_name}_{leg_state.name.replace(' ', '_')}"
                        )
                        if success:
                            leg_logger.info(f"✅ SL order modified on broker: {sl_order_id} @ ₹{new_sl_price:.2f}")

                            # Log to database
                            if self.persistence:
                                self.persistence.log_event(
                                    "SL_ORDER_MODIFIED",
                                    f"Leg {leg_state.leg_num} SL order modified on broker",
                                    metadata={
                                        'leg_num': leg_state.leg_num,
                                        'sl_order_id': leg_state.sl_order_id,
                                        'old_sl': old_sl,
                                        'new_sl': new_sl_price,
                                        'current_profit_pct': pnl_pct
                                    }
                                )
                        else:
                            leg_logger.warning(f"⚠️ Failed to modify SL order on broker")

                    if self.persistence:
                        self.persistence.log_sl_update(leg_state.leg_num, old_sl, new_sl_price)

        # ===== 2. CHECK AUTO-CLOSE (HIGHEST PRIORITY) =====
        auto_close_pct = safe_float(get_param('auto_close_profit_pct', allow_strategy_fallback=False))
        if auto_close_pct is not None and pnl_pct >= auto_close_pct:
            leg_logger.info(f"✅ Auto-close at {auto_close_pct}% profit: {pnl_pct:.2f}%")
            self._exit_leg_position(leg_state, current_price, f"AUTO_CLOSE_{auto_close_pct}PCT", leg_logger)
            return

        # ===== 3. PROFIT MANAGEMENT - DETERMINE MODE =====
        # CRITICAL FIX: Disable strategy fallback for mode-specific params
        # This prevents Leg 3 (null config) from inheriting Leg 1's profit lock config
        first_lock_pct = safe_float(get_param('first_lock_pct', allow_strategy_fallback=False))
        trail_trigger_pct = safe_float(get_param('trail_trigger_pct', allow_strategy_fallback=False))
        trail_move_pct = safe_float(get_param('trail_move_pct', allow_strategy_fallback=False))
        lock_profit_pct = safe_float(get_param('lock_profit_pct', allow_strategy_fallback=False))
        profit_lock_step = safe_float(get_param('profit_lock_step', allow_strategy_fallback=False))
        profit_step_threshold = safe_float(get_param('profit_step_threshold', allow_strategy_fallback=False))

        # MODE 2: Lock Once Then Trail (RECOMMENDED)
        # New behavior: Trigger lock at X% profit, set exit at Y%, then trail
        # CRITICAL: These params should NOT fallback to strategy defaults to avoid leg config pollution
        if first_lock_pct is not None and trail_trigger_pct is not None and trail_move_pct is not None:
            # Get lock trigger (profit % at which to activate lock)
            # If not specified, defaults to first_lock_pct (backward compatible)
            lock_trigger_pct = safe_float(get_param('lock_trigger_pct', first_lock_pct))

            # Initialize profit exit target on first call
            if leg_state.profit_exit_target is None:
                leg_state.profit_exit_target = None  # Will be set when lock triggers
                leg_state.last_trail_level = 0.0
                leg_state.first_lock_achieved = False

            # Stage 1: Check if lock should be triggered (profit reaches lock_trigger_pct)
            if not leg_state.first_lock_achieved and pnl_pct >= lock_trigger_pct:
                # Lock triggered - set exit target at first_lock_pct
                leg_state.first_lock_achieved = True
                leg_state.profit_exit_target = first_lock_pct
                leg_state.last_trail_level = pnl_pct

                leg_logger.info(f"🔒 Profit lock triggered at {pnl_pct:.2f}% (trigger: {lock_trigger_pct}%)")
                leg_logger.info(f"   Exit target set to {first_lock_pct}% - Will exit if profit falls to this level")
                leg_logger.info(f"   Will trail exit by {trail_move_pct}% every {trail_trigger_pct}% profit increase")

                # MODIFY existing SL order to lock profit (don't place new LIMIT order)
                # This ensures only ONE order on broker that trails up with profit
                profit_lock_price = leg_state.entry_price * (1 + first_lock_pct / 100)

                if leg_state.sl_order_id:
                    old_sl = leg_state.current_sl
                    leg_state.current_sl = profit_lock_price  # Update local SL to lock level

                    success = self.order_manager.modify_sl_order(
                        order_id=leg_state.sl_order_id,
                        symbol=leg_state.symbol,
                        quantity=leg_state.quantity,
                        new_stop_price=profit_lock_price,
                        strategy_name=f"{self.strategy_name}_{leg_state.name.replace(' ', '_')}"
                    )
                    if success:
                        leg_logger.info(f"✅ SL order modified to lock profit: ₹{old_sl:.2f} → ₹{profit_lock_price:.2f}")
                        leg_logger.info(f"   SL now protects {first_lock_pct}% profit (was {((old_sl - leg_state.entry_price) / leg_state.entry_price * 100):.1f}%)")
                    else:
                        leg_logger.warning(f"⚠️ Failed to modify SL to lock profit - SL remains at ₹{old_sl:.2f}")
                else:
                    leg_logger.warning(f"⚠️ No SL order ID found to modify for profit lock")

                # Log to database
                if self.persistence:
                    self.persistence.log_event(
                        "PROFIT_LOCK_TRIGGERED",
                        f"Leg {leg_state.leg_num} profit lock triggered at {pnl_pct:.2f}%",
                        metadata={
                            'leg_num': leg_state.leg_num,
                            'current_profit_pct': pnl_pct,
                            'lock_trigger_pct': lock_trigger_pct,
                            'exit_target_pct': first_lock_pct,
                            'trail_trigger_pct': trail_trigger_pct,
                            'trail_move_pct': trail_move_pct,
                            'current_price': current_price
                        }
                    )

            # Stage 2: Check if price fell back to exit target (after lock triggered)
            if leg_state.first_lock_achieved and leg_state.profit_exit_target is not None:
                if pnl_pct <= leg_state.profit_exit_target:
                    # Price fell to exit target - EXIT NOW
                    leg_logger.info(f"✅ Trailing exit triggered: Profit {pnl_pct:.2f}% fell to target {leg_state.profit_exit_target:.2f}%")
                    self._exit_leg_position(leg_state, current_price, f"TRAIL_EXIT_{leg_state.profit_exit_target:.1f}PCT", leg_logger)
                    return

            # Stage 3: Trail the exit target UP as profit increases (PROGRESSIVE LOCK)
            # User wants: 6% profit → lock 2%, 8% profit → lock 4%, 10% profit → lock 6%
            if leg_state.first_lock_achieved and leg_state.profit_exit_target is not None:
                profit_increase = pnl_pct - leg_state.last_trail_level

                if profit_increase >= trail_trigger_pct:
                    # Calculate how many intervals we've crossed
                    intervals_crossed = int(profit_increase / trail_trigger_pct)

                    # Move exit target UP by trail_move_pct * intervals (CHANGED FROM DOWN)
                    old_target = leg_state.profit_exit_target
                    new_target = leg_state.profit_exit_target + (trail_move_pct * intervals_crossed)

                    # Cap at current profit minus a small buffer to avoid immediate exit
                    max_exit_target = pnl_pct - 0.5  # Stay 0.5% below current profit
                    if new_target > max_exit_target:
                        new_target = max_exit_target
                        leg_logger.info(f"📈 Trail profit lock: {old_target:.2f}% → {new_target:.2f}% "
                                      f"(Profit at {pnl_pct:.2f}%) - CAPPED at profit buffer")
                    else:
                        leg_logger.info(f"📈 Trail profit lock: {old_target:.2f}% → {new_target:.2f}% "
                                      f"(Profit at {pnl_pct:.2f}%)")

                    leg_state.profit_exit_target = new_target
                    leg_state.last_trail_level = pnl_pct

                    # MODIFY SL order to trail the profit lock UP (not a separate LIMIT order)
                    # This keeps only ONE order on broker that protects progressively more profit
                    if leg_state.sl_order_id:
                        old_sl = leg_state.current_sl
                        new_sl_price = leg_state.entry_price * (1 + new_target / 100)
                        leg_state.current_sl = new_sl_price  # Update local SL

                        success = self.order_manager.modify_sl_order(
                            order_id=leg_state.sl_order_id,
                            symbol=leg_state.symbol,
                            quantity=leg_state.quantity,
                            new_stop_price=new_sl_price,
                            strategy_name=f"{self.strategy_name}_{leg_state.name.replace(' ', '_')}"
                        )
                        if success:
                            leg_logger.info(f"✅ SL trailed to lock more profit: ₹{old_sl:.2f} → ₹{new_sl_price:.2f} (protects {new_target:.1f}% profit)")
                        else:
                            # SL order not modifiable (may have executed) - position likely already closed
                            leg_logger.warning(f"⚠️ Could not trail SL - order may have executed")
                            # Don't clear sl_order_id here, exit handler will clean up
                    else:
                        leg_logger.warning(f"⚠️ No SL order to trail (should not happen in normal flow)")

                    # Log to database
                    if self.persistence:
                        self.persistence.log_event(
                            "PROFIT_EXIT_TRAILED",
                            f"Leg {leg_state.leg_num} exit target trailed: {old_target:.2f}% → {leg_state.profit_exit_target:.2f}%",
                            metadata={
                                'leg_num': leg_state.leg_num,
                                'old_exit_target': old_target,
                                'new_exit_target': leg_state.profit_exit_target,
                                'current_profit_pct': pnl_pct,
                                'current_price': current_price,
                                'intervals_crossed': intervals_crossed
                            }
                        )

        # MODE 3: Progressive Escalating Lock (OLD BEHAVIOR)
        elif profit_lock_step is not None and profit_step_threshold is not None and lock_profit_pct is not None:
            # Initialize escalation tracking
            if not hasattr(leg_state, 'profit_level_for_lock_increase'):
                leg_state.profit_level_for_lock_increase = lock_profit_pct + profit_step_threshold

            # Escalate target when crossing thresholds
            if pnl_pct >= leg_state.profit_level_for_lock_increase:
                lock_profit_pct += profit_lock_step
                leg_state.profit_level_for_lock_increase += profit_step_threshold
                leg_logger.info(f"🔒 Profit lock escalated to {lock_profit_pct:.2f}%")
                leg_state.config['lock_profit_pct'] = lock_profit_pct

            # Check if profit target reached
            if pnl_pct >= lock_profit_pct:
                leg_logger.info(f"✅ Profit lock reached: {pnl_pct:.2f}% (target: {lock_profit_pct:.2f}%)")
                self._exit_leg_position(leg_state, current_price, "PROFIT_LOCK", leg_logger)

        # MODE 1: Simple Profit Lock (DEFAULT)
        elif lock_profit_pct is not None:
            if pnl_pct >= lock_profit_pct:
                leg_logger.info(f"✅ Profit lock reached: {pnl_pct:.2f}% (target: {lock_profit_pct:.2f}%)")
                self._exit_leg_position(leg_state, current_price, "PROFIT_LOCK", leg_logger)

    def _exit_leg_position(self, leg_state: LegState, exit_price: float,
                          reason: str, leg_logger):
        """Exit position (atomic, prevents duplicate exits)"""
        try:
            # ATOMIC CHECK: Only one thread can proceed with exit
            with self.state_lock:
                # Check if already exiting or already exited
                if getattr(leg_state, '_exiting', False) or not leg_state.is_active:
                    leg_logger.debug(f"Position already closing/closed, skipping duplicate exit")
                    return

                # Mark as exiting immediately (prevents other threads from entering)
                leg_state._exiting = True
                leg_state.is_active = False

            # Store local copies of order IDs for consistent logging
            sl_order_id = leg_state.sl_order_id
            profit_target_order_id = leg_state.profit_target_order_id

            # Calculate P&L FIRST (before any modifications)
            pnl, pnl_pct = leg_state.calculate_pnl(exit_price)

            # Cancel SL order on broker (best-effort)
            # NOTE: This is the ONLY order we manage - SL gets modified to lock profits
            if sl_order_id:
                try:
                    success = self.order_manager.cancel_sl_order(
                        order_id=sl_order_id,
                        strategy_name=f"{self.strategy_name}_{leg_state.name.replace(' ', '_')}"
                    )
                    if success:
                        leg_logger.info(f"✅ SL order canceled on broker: {sl_order_id}")
                    else:
                        leg_logger.warning(f"⚠️ SL order cancellation returned False: {sl_order_id}")
                except Exception as e:
                    # Don't fail exit if cancel fails (order might have already executed)
                    leg_logger.warning(f"⚠️ SL order cancellation failed: {e}")
                finally:
                    # Clear local reference regardless
                    leg_state.sl_order_id = None

            # Note: No separate profit target order to cancel - we modify SL for profit lock
            # Clear profit_target_order_id if it was set (legacy/transition)
            if profit_target_order_id:
                leg_logger.debug(f"Clearing legacy profit_target_order_id: {profit_target_order_id}")
                leg_state.profit_target_order_id = None

            # Place exit order (this is the actual SELL to close position)
            try:
                order_id = self.order_manager.place_order(
                    symbol=leg_state.symbol,
                    quantity=leg_state.quantity,
                    action=self.order_manager.exit_action
                )
                if not order_id:
                    leg_logger.error("Exit order placement returned no order ID")
            except Exception as e:
                leg_logger.error(f"Exit order placement error: {e}")

            # Record exit details
            leg_state.exit_price = exit_price
            leg_state.exit_reason = reason

            # Print comprehensive exit summary ONCE
            leg_logger.info("=" * 80)
            leg_logger.info(f"✅ POSITION CLOSED - {reason}")
            leg_logger.info("=" * 80)
            leg_logger.info(f"Symbol:          {leg_state.symbol}")

            # Show lot breakdown
            lot = leg_state.config.get('lot', 1)
            lot_multiplier = leg_state.strategy_defaults.get('lot_multiplier', 1)
            leg_logger.info(f"Lot:             {lot} | Multiplier: {lot_multiplier}x | Quantity: {leg_state.quantity}")

            leg_logger.info(f"Entry Price:     ₹{leg_state.entry_price:.2f}")
            leg_logger.info(f"Exit Price:      ₹{exit_price:.2f}")
            leg_logger.info(f"Price Change:    ₹{exit_price - leg_state.entry_price:+.2f} ({pnl_pct:+.2f}%)")
            leg_logger.info(f"Initial SL:      ₹{leg_state.entry_price * (1 - leg_state.initial_sl_pct / 100):.2f} (-{leg_state.initial_sl_pct}%)")
            leg_logger.info(f"Final SL:        ₹{leg_state.current_sl:.2f}")
            leg_logger.info(f"Highest Price:   ₹{leg_state.highest_price:.2f}")
            leg_logger.info(f"Total P&L:       ₹{pnl:,.2f} ({pnl_pct:+.2f}%)")
            leg_logger.info("=" * 80)

            # Persist exit to DB
            if self.persistence:
                self.persistence.record_leg_exit(
                    leg_state.leg_num,
                    exit_price,
                    reason,
                    realized_pnl=pnl,
                    pnl_percentage=pnl_pct
                )

                # Log to database
                self.persistence.log_event(
                    "POSITION_CLOSED",
                    f"Leg {leg_state.leg_num} position closed: {reason}",
                    metadata={
                        'leg_num': leg_state.leg_num,
                        'symbol': leg_state.symbol,
                        'entry_price': leg_state.entry_price,
                        'exit_price': exit_price,
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'reason': reason
                    }
                )

        except Exception as e:
            leg_logger.error(f"Exit error: {e}", exc_info=True)

    def _print_summary(self):
        """Print strategy summary"""
        logger.info("\n" + "="*70)
        logger.info("ðŸ“Š STRATEGY SUMMARY")
        logger.info("="*70)

        for leg_num, leg_state in self.leg_states.items():
            if leg_state.exit_price:
                pnl, pnl_pct = leg_state.calculate_pnl(leg_state.exit_price)
                logger.info(f"{leg_state.name}: {leg_state.symbol}")
                logger.info(f"  Entry: â‚¹{leg_state.entry_price:.2f}, Exit: â‚¹{leg_state.exit_price:.2f}")
                logger.info(f"  P&L: â‚¹{pnl:.2f} ({pnl_pct:+.2f}%), Reason: {leg_state.exit_reason}")
            else:
                logger.info(f"{leg_state.name}: Not entered")

        logger.info("="*70 + "\n")
