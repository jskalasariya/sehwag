"""
Position Management Module
==========================
Handles position tracking, SL management, and exit logic.
"""

import logging
from typing import Optional, Dict
from datetime import datetime

from .models import LegPosition, StrategyState
from .order_manager import OrderManager

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages position lifecycle and risk management"""
    
    def __init__(self, order_manager: OrderManager, config: Dict):
        """
        Initialize position manager
        
        Args:
            order_manager: OrderManager instance
            config: Configuration dictionary
        """
        self.order_manager = order_manager
        self.config = config
        self.entry_action = config.get('entry_action', 'BUY')
        self.exit_action = config.get('exit_action', 'SELL')
    
    def manage_position(self, leg_num: int, leg_config: Dict, 
                       state: StrategyState, current_price: float) -> None:
        """
        Unified position management with flexible configuration

        Each leg can independently configure:
        - Stop Loss (fixed or trailing)
        - Profit locking (simple or escalating)
        - Auto-close at target profit

        Args:
            leg_num: Leg number
            leg_config: Leg configuration
            state: Strategy state
            current_price: Current market price
        """
        leg = state.get_position(leg_num)
        if not leg or not leg.is_active:
            return
        
        pnl, pnl_pct = leg.calculate_pnl(current_price, self.entry_action)
        
        leg_name = leg_config.get('name', f'Leg {leg_num}')
        logger.debug(f"{leg_name}: PnL {pnl_pct:.2f}%, SL: {leg.current_sl:.2f}, LTP: {current_price:.2f}")
        
        # 1. UPDATE TRAILING STOP LOSS (if configured)
        sl_trail_pct = leg_config.get('sl_trail_pct')
        if sl_trail_pct and pnl_pct > 0:
            # Trail SL by keeping it sl_trail_pct% below current price
            new_sl = current_price * (1 - sl_trail_pct / 100)
            if new_sl > leg.current_sl:
                leg.current_sl = new_sl
                logger.info(f"📈 {leg_name} trailing SL updated to {leg.current_sl:.2f} (LTP: {current_price:.2f})")

        # 2. CHECK STOP LOSS BREACH
        if current_price <= leg.current_sl:
            logger.warning(f"⚠️  {leg_name} SL breached at {current_price:.2f}")
            self.exit_position(leg, current_price, "SL_BREACH")
            return
        
        # 3. UPDATE ESCALATING PROFIT LOCK (if configured)
        profit_lock_step = leg_config.get('profit_lock_step')
        profit_step_threshold = leg_config.get('profit_step_threshold')

        if profit_lock_step and profit_step_threshold:
            # Escalate profit lock when profit crosses thresholds
            if pnl_pct >= leg.lock_profit_pct and pnl_pct >= leg.profit_level_for_lock_increase:
                leg.lock_profit_pct += profit_lock_step
                leg.profit_level_for_lock_increase += profit_step_threshold
                logger.info(f"🔒 {leg_name} lock profit escalated to {leg.lock_profit_pct:.2f}%")

        # 4. CHECK AUTO-CLOSE AT PROFIT TARGET
        auto_close_pct = leg_config.get('auto_close_profit_pct')
        if auto_close_pct and pnl_pct >= auto_close_pct:
            logger.info(f"✅ {leg_name} auto-close at {auto_close_pct}% profit: {pnl_pct:.2f}%")
            self.exit_position(leg, current_price, f"AUTO_CLOSE_{auto_close_pct}PCT")
            return

        # 5. CHECK PROFIT LOCK TARGET
        if pnl_pct >= leg.lock_profit_pct:
            logger.info(f"✅ {leg_name} profit lock reached: {pnl_pct:.2f}% (target: {leg.lock_profit_pct:.2f}%)")
            self.exit_position(leg, current_price, "PROFIT_LOCK")

    def exit_position(self, leg: LegPosition, exit_price: float, reason: str) -> None:
        """
        Exit a position
        
        Args:
            leg: Leg position to exit
            exit_price: Exit price
            reason: Exit reason
        """
        if not leg.is_active:
            return
        
        logger.info(f"🚪 Exiting Leg {leg.leg_id}: {reason} at {exit_price:.2f}")
        
        # Place exit order (with position verification)
        order_id = self.order_manager.place_order(
            leg.symbol, 
            leg.quantity, 
            self.exit_action
        )
        
        if order_id:
            leg.is_active = False
            leg.exit_price = exit_price
            leg.exit_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            leg.pnl, leg.pnl_pct = leg.calculate_pnl(exit_price, self.entry_action)
            
            logger.info(f"✅ Leg {leg.leg_id} exited - PnL: {leg.pnl_pct:.2f}% (₹{leg.pnl:.2f})")
        else:
            # Order failed or position already closed
            logger.warning(f"⚠️  Exit order not placed for Leg {leg.leg_id}")
            logger.warning(f"   Position likely closed manually - marking as inactive")
            leg.is_active = False
            leg.exit_price = exit_price
            leg.exit_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            leg.pnl, leg.pnl_pct = leg.calculate_pnl(exit_price, self.entry_action)
            logger.info(f"ℹ️  Leg {leg.leg_id} marked as closed - Estimated PnL: {leg.pnl_pct:.2f}% (₹{leg.pnl:.2f})")
