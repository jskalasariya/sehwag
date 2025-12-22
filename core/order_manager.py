"""
Order Execution Module
======================
Handles order placement and execution tracking.
Includes SL order management following Expiry Blast standards.
"""

import logging
from typing import Optional, Dict
import time

logger = logging.getLogger(__name__)


def round_to_tick_size(price: float, tick_size: float = 0.05) -> float:
    """Round price to nearest tick size"""
    return round(price / tick_size) * tick_size


class OrderManager:
    """Manages order placement and execution including SL orders"""

    def __init__(self, client, config: Dict):
        """
        Initialize order manager
        
        Args:
            client: OpenAlgo API client
            config: Configuration dictionary
        """
        self.client = client
        self.config = config
        
        self.test_mode = config.get('test_mode', False)
        self.auto_place_orders = config.get('auto_place_orders', False)
        self.exchange = config.get('option_exchange', 'NFO')
        self.price_type = config.get('price_type', 'MARKET')
        self.product = config.get('product', 'NRML')
        self.instrument_type = config.get('instrument_type', 'options')
        self.entry_action = config.get('entry_action', 'BUY')
        self.exit_action = config.get('exit_action', 'SELL')

        # Strategy name for order tagging (derived from underlying if not set)
        self.strategy_name = config.get('strategy_name', 'sehwag')

        # SL Order configuration
        self.place_sl_order_enabled = config.get('place_sl_order', True)
        self.sl_order_type = config.get('sl_order_type', 'SL')  # SL-Limit
        self.sl_limit_buffer_percent = config.get('sl_limit_buffer_percent', 0.015)  # 1.5% default
        self.sl_use_percent_buffer = config.get('sl_use_percent_buffer', True)

    def verify_position_exists(self, symbol: str) -> bool:
        """
        Verify if a position exists in broker's position book (called before exit orders only).

        Args:
            symbol: Symbol to check

        Returns:
            True if position exists, False otherwise
        """
        try:
            # Skip check in test/sim modes
            if self.test_mode or not self.auto_place_orders:
                return True

            # Call positionbook API
            resp = self.client.positionbook()

            # Handle error responses
            if not isinstance(resp, dict):
                logger.warning(f"Position verification failed: Invalid response type")
                return True  # Allow order to proceed if API fails

            if resp.get('status') != 'success':
                logger.warning(f"Position verification failed: {resp.get('message', 'Unknown error')}")
                return True  # Allow order to proceed if API fails

            # Get positions data
            positions_data = resp.get('data', [])
            if not positions_data:
                logger.warning(f"⚠️  No positions found in position book - position may already be closed")
                return False

            # Check if symbol exists in positions with non-zero quantity
            for position in positions_data:
                # Handle different broker response formats
                pos_symbol = position.get('symbol') or position.get('tradingsymbol') or position.get('Symbol')
                quantity = position.get('quantity') or position.get('netqty') or position.get('Quantity') or 0

                try:
                    quantity = int(quantity)
                except (ValueError, TypeError):
                    quantity = 0

                if pos_symbol == symbol and quantity != 0:
                    logger.debug(f"✓ Position verified: {symbol} (Qty: {quantity})")
                    return True

            logger.warning(f"⚠️  Position not found for {symbol} - may have been manually closed")
            return False

        except Exception as e:
            logger.error(f"Error verifying position for {symbol}: {e}")
            return True  # Allow order to proceed if check fails

    def place_order(self, symbol: str, quantity: int, action: str) -> Optional[str]:
        """
        Place an order (with position verification for exit orders).

        Args:
            symbol: Symbol to trade
            quantity: Quantity to trade
            action: "BUY" or "SELL"
            
        Returns:
            Order ID or None if failed
        """
        try:
            # SAFETY CHECK: Verify position exists before placing exit orders
            if action == self.exit_action:
                if not self.verify_position_exists(symbol):
                    logger.warning(f"🚫 EXIT ORDER SKIPPED: Position for {symbol} not found (manually closed?)")
                    logger.warning(f"   This prevents duplicate exit orders")
                    return None

            logger.info(f"📊 Placing {action} order: {symbol}, Qty: {quantity}")
            
            if self.test_mode:
                logger.info(f"🧪 TEST MODE - Order NOT placed (simulated): {action} {quantity} {symbol}")
                return f"TEST_ORDER_{symbol}_{int(time.time())}"
            
            if not self.auto_place_orders:
                logger.warning(f"⚠️  AUTO_PLACE_ORDERS is False - Order simulated: {action} {quantity} {symbol}")
                return f"SIM_ORDER_{symbol}_{int(time.time())}"
            
            resp = self.client.placeorder(
                strategy="nifty_sehwag",
                symbol=symbol,
                exchange=self.exchange,
                action=action,
                quantity=quantity,
                price_type=self.price_type,
                product=self.product
            )
            
            if resp.get('status') == 'success':
                order_id = resp.get('orderid')
                logger.info(f"✓ Order placed successfully: {order_id}")
                return order_id
            else:
                error_msg = resp.get('message', 'Unknown error')
                logger.error(f"✗ Order placement failed: {error_msg}")
                logger.error(f"   Full response: {resp}")
                return None
        
        except Exception as e:
            logger.error(f"✗ Error placing order: {e}")
            return None
    
    def check_order_status(self, order_id: str) -> Optional[str]:
        """
        Check order status (simple status string)

        Args:
            order_id: Order ID to check

        Returns:
            Order status string ('COMPLETE', 'PENDING', 'REJECTED', etc.) or None
        """
        order_info = self.get_order_status(order_id)
        if order_info:
            # Try multiple field names for status
            status = (
                order_info.get('order_status') or
                order_info.get('orderstatus') or
                order_info.get('status') or
                order_info.get('Status')
            )
            return status
        return None

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        """
        Get status of an order
        
        Args:
            order_id: Order ID to check
            
        Returns:
            Order status dict or None
        """
        try:
            if self.test_mode or order_id.startswith("TEST_") or order_id.startswith("SIM_"):
                return {
                    'status': 'COMPLETE',
                    'order_id': order_id,
                    'filled_quantity': 0,
                    'average_price': 0.0
                }
            
            resp = self.client.orderbook()

            # Handle string error responses (API errors)
            if not isinstance(resp, dict):
                logger.error(f"Orderbook returned non-dict response: {type(resp).__name__}")
                return None

            # DEBUG: Log complete orderbook response structure (first time only)
            if not hasattr(self, '_logged_orderbook_structure'):
                logger.info("=" * 80)
                logger.info(f"📋 COMPLETE ORDERBOOK API RESPONSE (for debugging):")
                logger.info(f"   Response type: {type(resp).__name__}")
                logger.info(f"   Response keys: {list(resp.keys()) if isinstance(resp, dict) else 'N/A'}")
                logger.info(f"   Status: {resp.get('status')}")
                logger.info(f"   Data type: {type(resp.get('data')).__name__}")
                if isinstance(resp.get('data'), dict):
                    logger.info(f"   Data keys (first 10): {list(resp.get('data', {}).keys())[:10]}")
                elif isinstance(resp.get('data'), list):
                    logger.info(f"   Data length: {len(resp.get('data', []))}")
                    if resp.get('data'):
                        logger.info(f"   First order keys: {list(resp['data'][0].keys()) if isinstance(resp['data'][0], dict) else 'N/A'}")
                logger.info(f"   Full response (first 500 chars): {str(resp)[:500]}")
                logger.info("=" * 80)
                self._logged_orderbook_structure = True

            if resp.get('status') == 'success':
                data = resp.get('data', {})

                # Handle nested structure: data -> orders -> [list]
                if isinstance(data, dict) and 'orders' in data:
                    orders = data.get('orders', [])
                    logger.debug(f"Found nested 'orders' array with {len(orders) if isinstance(orders, list) else 0} orders")
                else:
                    # Fallback: data might be the orders list directly
                    orders = data if isinstance(data, list) else []
                    logger.debug(f"Using data directly as orders list")

                # Iterate through orders list
                if isinstance(orders, list):
                    for order in orders:
                        if isinstance(order, dict):
                            # Match by orderid
                            if order.get('orderid') == order_id or order.get('order_id') == order_id:
                                logger.debug(f"Found order {order_id} in orderbook")
                                return order

                    logger.debug(f"Order {order_id} not found in orderbook (may still be processing)")
                    return None
                else:
                    logger.error(f"Orders is not a list: {type(orders).__name__}")
                    return None
            else:
                logger.error(f"Failed to get orderbook: {resp}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting order status for {order_id}: {e}")
            return None

    def _get_fill_price_from_tradebook(self, order_id: str, custom_logger=None) -> Optional[float]:
        """
        Fetch actual fill price from tradebook (for MARKET orders)

        Args:
            order_id: Order ID to check
            custom_logger: Optional logger to use (defaults to module logger)

        Returns:
            Actual execution price or None
        """
        log = custom_logger if custom_logger else logger
        try:
            resp = self.client.tradebook()

            if not isinstance(resp, dict):
                log.debug(f"Tradebook returned non-dict response: {type(resp).__name__}")
                return None

            # DEBUG: Log tradebook structure (first time only)
            if not hasattr(self, '_logged_tradebook_structure'):
                log.info("=" * 80)
                log.info(f"📋 TRADEBOOK API RESPONSE STRUCTURE (for debugging):")
                log.info(f"   Response type: {type(resp).__name__}")
                log.info(f"   Response keys: {list(resp.keys()) if isinstance(resp, dict) else 'N/A'}")
                log.info(f"   Status: {resp.get('status')}")
                log.info(f"   Data type: {type(resp.get('data')).__name__}")
                if isinstance(resp.get('data'), dict):
                    log.info(f"   Data keys: {list(resp.get('data', {}).keys())}")
                elif isinstance(resp.get('data'), list):
                    log.info(f"   Data length: {len(resp.get('data', []))}")
                    if resp.get('data'):
                        log.info(f"   First trade keys: {list(resp['data'][0].keys()) if isinstance(resp['data'][0], dict) else 'N/A'}")
                log.info(f"   Full response (first 500 chars): {str(resp)[:500]}")
                log.info("=" * 80)
                self._logged_tradebook_structure = True

            if resp.get('status') != 'success':
                log.debug(f"Tradebook API status not success: {resp.get('status')}")
                return None

            data = resp.get('data', {})

            # Handle nested structure similar to orderbook: data -> trades -> [list]
            if isinstance(data, dict) and 'trades' in data:
                trades = data.get('trades', [])
                log.debug(f"Found nested 'trades' array with {len(trades) if isinstance(trades, list) else 0} trades")
            else:
                # Fallback: data might be the trades list directly
                trades = data if isinstance(data, list) else []
                log.debug(f"Using data directly as trades list")

            if not isinstance(trades, list):
                log.debug(f"Trades is not a list: {type(trades).__name__}")
                return None

            # Find trade matching this order_id
            for trade in trades:
                if isinstance(trade, dict):
                    trade_order_id = trade.get('orderid') or trade.get('order_id')
                    if trade_order_id == order_id:
                        # DEBUG: Log the trade structure we found
                        log.debug(f"Found trade for order {order_id}: {trade}")

                        # Found the trade - try multiple field names for fill price
                        fill_price = (
                            trade.get('averageprice') or
                            trade.get('average_price') or
                            trade.get('avgprice') or
                            trade.get('price') or
                            trade.get('fillprice') or
                            trade.get('fill_price') or
                            trade.get('tradeprice')
                        )

                        if fill_price:
                            try:
                                fill_price_float = float(fill_price)
                                if fill_price_float > 0:
                                    log.debug(f"Extracted fill price ₹{fill_price_float:.2f} from tradebook")
                                    return fill_price_float
                            except (ValueError, TypeError) as e:
                                log.debug(f"Could not convert fill price '{fill_price}' to float: {e}")

                        # Trade found but no valid price field
                        log.warning(f"⚠️ Trade found for order {order_id} but no valid price field")
                        log.warning(f"   Trade data: {trade}")
                        log.warning(f"   Available fields: {list(trade.keys())}")
                        return None

            log.debug(f"Order {order_id} not found in tradebook (may not have executed yet)")
            return None

        except Exception as e:
            log.debug(f"Error fetching from tradebook: {e}")
            return None

    def get_fill_price(self, order_id: str, max_wait_seconds: int = 5, custom_logger=None) -> Optional[float]:
        """
        Fetch actual fill price from broker orderbook after order execution

        Args:
            order_id: Order ID to check
            max_wait_seconds: Maximum time to wait for order to execute
            custom_logger: Optional logger to use (e.g., leg_logger for leg-level logs)

        Returns:
            Average fill price or None if not found/not filled
        """
        log = custom_logger if custom_logger else logger
        try:
            if self.test_mode or order_id.startswith("TEST_") or order_id.startswith("SIM_"):
                log.debug(f"Test/Sim mode - cannot fetch real fill price for {order_id}")
                return None

            # Poll orderbook for fill price
            start_time = time.time()
            poll_interval = 0.5  # Check every 500ms
            attempt = 0
            max_attempts = int(max_wait_seconds / poll_interval)

            while (time.time() - start_time) < max_wait_seconds:
                attempt += 1
                order_status = self.get_order_status(order_id)

                if order_status:
                    # DEBUG: Print full order structure on first attempt
                    if attempt == 1:
                        log.info("=" * 80)
                        log.info(f"📋 ORDERBOOK RESPONSE FOR ORDER {order_id}:")
                        log.info(f"   Full order data: {order_status}")
                        log.info(f"   Available keys: {list(order_status.keys())}")
                        log.info("=" * 80)

                    # Check order status (field name is 'order_status', not 'status')
                    status = (order_status.get('order_status') or order_status.get('status') or '').lower()

                    if status in ['complete', 'executed', 'filled']:
                        # For MARKET orders, orderbook shows price=0.0
                        # Need to fetch from tradebook for actual execution price

                        # First try: Use price from orderbook (works for LIMIT/SL orders)
                        orderbook_price = order_status.get('price')

                        if orderbook_price and float(orderbook_price) > 0:
                            try:
                                fill_price_float = float(orderbook_price)
                                log.info(f"✅ Fill price from orderbook: ₹{fill_price_float:.2f} (order {order_id})")
                                return fill_price_float
                            except (ValueError, TypeError):
                                pass

                        # Second try: Fetch from tradebook (for MARKET orders)
                        log.debug(f"Orderbook price is 0.0, fetching from tradebook...")
                        tradebook_price = self._get_fill_price_from_tradebook(order_id, custom_logger=log)

                        if tradebook_price and tradebook_price > 0:
                            log.info(f"✅ Fill price from tradebook: ₹{tradebook_price:.2f} (order {order_id})")
                            return tradebook_price

                        # Third try: Alternative field names (backward compatibility)
                        fill_price = (
                            order_status.get('averageprice') or
                            order_status.get('average_price') or
                            order_status.get('avgprice') or
                            order_status.get('filled_price')
                        )

                        if fill_price:
                            try:
                                fill_price_float = float(fill_price)
                                if fill_price_float > 0:
                                    log.info(f"✅ Fill price from alt field: ₹{fill_price_float:.2f} (order {order_id})")
                                    return fill_price_float
                            except (ValueError, TypeError):
                                pass

                        # No valid price found
                        log.warning(f"⚠️ Order {order_id} is {status} but no valid fill price found")
                        log.debug(f"   orderbook price: {orderbook_price}, tradebook price: {tradebook_price}")
                        return None

                    # If order is rejected or cancelled, stop waiting
                    if status in ['rejected', 'cancelled', 'canceled', 'failed']:
                        log.error(f"✗ Order {order_id} was {status}, cannot get fill price")
                        return None

                    # Order exists but not yet filled - log status on first and last attempt
                    if attempt == 1 or attempt == max_attempts:
                        log.debug(f"Order {order_id} status: {status} (attempt {attempt}/{max_attempts})")

                # Wait before next poll
                time.sleep(poll_interval)

            # Timeout - log as debug not warning to reduce noise
            log.debug(f"Could not fetch fill price for order {order_id} after {max_wait_seconds}s (orderbook may be delayed)")
            return None

        except Exception as e:
            log.error(f"✗ Error fetching fill price for {order_id}: {e}")
            return None

    def place_sl_order(self, symbol: str, quantity: int, stop_price: float, strategy_name: str = "nifty_sehwag") -> Optional[str]:
        """
        Place a stop-loss order on the broker

        Args:
            symbol: Symbol to place SL for
            quantity: Quantity
            stop_price: Stop loss trigger price
            strategy_name: Strategy identifier

        Returns:
            SL Order ID or None if failed
        """
        if not self.auto_place_orders or not self.place_sl_order_enabled:
            return None

        if self.test_mode:
            logger.info(f"🧪 TEST MODE - Simulated SL order @ ₹{stop_price:.2f}")
            return f"TEST_SL_{symbol}_{int(time.time())}"

        try:
            # Round to tick size to avoid broker rejection
            rounded_trigger = round_to_tick_size(stop_price)

            # Calculate buffer (percentage-based)
            buffer = rounded_trigger * self.sl_limit_buffer_percent

            # For SL order, limit price should be slightly worse than trigger (for SELL, lower)
            rounded_limit = round_to_tick_size(rounded_trigger - buffer)

            logger.info(f"📤 Placing SL-L order on broker: {symbol}")
            logger.info(f"   Trigger: ₹{rounded_trigger:.2f} | Limit: ₹{rounded_limit:.2f} (Buffer: ₹{buffer:.2f} / {(buffer/rounded_trigger)*100:.2f}%)")

            response = self.client.placeorder(
                strategy=f"{strategy_name}_SL",
                symbol=symbol,
                exchange=self.exchange,
                action=self.exit_action,
                price_type="SL",  # Stop Loss Limit order
                product=self.product,
                quantity=quantity,
                trigger_price=rounded_trigger,
                price=rounded_limit  # Limit price with buffer
            )

            if response.get('status') == 'success':
                order_id = response.get('orderid')
                logger.info(f"✅ SL order placed on broker! OrderID: {order_id} @ Trigger: ₹{stop_price:.2f}")

                # Verify order was actually placed (wait 1s for order to appear in orderbook)
                time.sleep(1)
                return order_id
            else:
                logger.error(f"✗ SL order placement failed: {response.get('message')}")
                return None

        except Exception as e:
            logger.error(f"✗ Exception placing SL order: {e}")
            return None

    def modify_sl_order(self, order_id: str, symbol: str, quantity: int, new_stop_price: float, strategy_name: str = "nifty_sehwag") -> bool:
        """
        Modify existing stop-loss order on the broker

        Args:
            order_id: Existing SL order ID
            symbol: Symbol
            quantity: Quantity
            new_stop_price: New stop loss trigger price
            strategy_name: Strategy identifier

        Returns:
            True if modified successfully, False otherwise
        """
        if not self.auto_place_orders or not self.place_sl_order_enabled or not order_id:
            return False

        if self.test_mode:
            logger.info(f"🧪 TEST MODE - Simulated SL modify to ₹{new_stop_price:.2f}")
            return True

        try:
            # Round to tick size to avoid broker rejection
            rounded_trigger = round_to_tick_size(new_stop_price)

            # Calculate buffer (percentage-based)
            buffer = rounded_trigger * self.sl_limit_buffer_percent

            # For SL order, limit price should be slightly worse than trigger (for SELL, lower)
            rounded_limit = round_to_tick_size(rounded_trigger - buffer)

            logger.info(f"📝 Modifying SL-L order {order_id}")
            logger.info(f"   New Trigger: ₹{rounded_trigger:.2f} | New Limit: ₹{rounded_limit:.2f} (Buffer: ₹{buffer:.2f} / {(buffer/rounded_trigger)*100:.2f}%)")

            response = self.client.modifyorder(
                strategy=f"{strategy_name}_SL",
                symbol=symbol,
                exchange=self.exchange,
                action=self.exit_action,
                price_type="SL",  # Stop Loss Limit order
                product=self.product,
                quantity=quantity,
                trigger_price=rounded_trigger,
                price=rounded_limit,  # Limit price with buffer
                order_id=order_id
            )

            if response.get('status') == 'success':
                logger.info(f"✅ SL order modified successfully! New trigger: ₹{new_stop_price:.2f}")
                return True
            else:
                error_msg = response.get('message', 'Unknown error')

                # Handle order already executed/completed (not an error - SL was hit!)
                if 'not a pending order' in error_msg.lower() or 'already executed' in error_msg.lower():
                    logger.info(f"ℹ️  SL order already executed or completed (cannot modify)")
                    return False  # Cannot modify but not an error condition
                else:
                    logger.error(f"✗ SL order modification failed: {error_msg}")
                    return False

        except Exception as e:
            logger.error(f"✗ Exception modifying SL order: {e}")
            return False

    def cancel_sl_order(self, order_id: str, strategy_name: str = "nifty_sehwag") -> bool:
        """
        Cancel stop-loss order on the broker

        Args:
            order_id: SL order ID to cancel
            strategy_name: Strategy identifier

        Returns:
            True if canceled successfully, False otherwise
        """
        if not self.auto_place_orders or not self.place_sl_order_enabled or not order_id:
            return False

        if self.test_mode:
            logger.info(f"🧪 TEST MODE - Simulated SL cancel")
            return True

        try:
            logger.info(f"🗑️ Canceling SL order {order_id}")

            response = self.client.cancelorder(
                strategy=f"{strategy_name}_SL",
                order_id=order_id
            )

            if response.get('status') == 'success':
                logger.info(f"✅ SL order canceled successfully")
                return True
            else:
                error_msg = response.get('message', 'Unknown error')

                # Check if order was already executed/completed (not an error condition)
                if 'not a pending order' in error_msg.lower() or 'already executed' in error_msg.lower():
                    logger.info(f"ℹ️  SL order already executed or completed (not pending)")
                    return True  # Not an error - order was executed
                else:
                    logger.error(f"✗ SL order cancellation failed: {error_msg}")
                    return False

        except Exception as e:
            logger.error(f"✗ Exception canceling SL order: {e}")
            return False

    def place_profit_target_order(self, symbol: str, quantity: int, target_price: float, strategy_name: str = "nifty_sehwag") -> Optional[str]:
        """
        Place a profit target limit order on the broker

        Args:
            symbol: Symbol to place target for
            quantity: Quantity
            target_price: Target profit price (limit order)
            strategy_name: Strategy identifier

        Returns:
            Order ID or None if failed
        """
        if not self.auto_place_orders or not self.place_sl_order_enabled:
            return None

        if self.test_mode:
            logger.info(f"🧪 TEST MODE - Simulated profit target @ ₹{target_price:.2f}")
            return f"TEST_TARGET_{symbol}_{int(time.time())}"

        try:
            # Round to tick size
            rounded_price = round_to_tick_size(target_price)

            logger.info(f"📤 Placing profit target order: {symbol}")
            logger.info(f"   Target: ₹{rounded_price:.2f} (LIMIT order)")

            response = self.client.placeorder(
                strategy=f"{strategy_name}_TARGET",
                symbol=symbol,
                exchange=self.exchange,
                action=self.exit_action,
                price_type="LIMIT",  # Limit order for profit target
                product=self.product,
                quantity=quantity,
                price=rounded_price
            )

            if response.get('status') == 'success':
                order_id = response.get('orderid')
                logger.info(f"✅ Profit target order placed! OrderID: {order_id} @ ₹{target_price:.2f}")
                time.sleep(1)
                return order_id
            else:
                error_msg = response.get('message', 'Unknown error')
                logger.error(f"✗ Profit target order failed: {error_msg}")
                return None

        except Exception as e:
            logger.error(f"✗ Exception placing profit target: {e}")
            return None

    def modify_profit_target_order(self, order_id: str, symbol: str, quantity: int, new_target_price: float, strategy_name: str = "nifty_sehwag") -> bool:
        """
        Modify existing profit target order

        Args:
            order_id: Existing target order ID
            symbol: Symbol
            quantity: Quantity
            new_target_price: New target price
            strategy_name: Strategy identifier

        Returns:
            True if modified successfully
        """
        if not self.auto_place_orders or not self.place_sl_order_enabled or not order_id:
            return False

        if self.test_mode:
            logger.info(f"🧪 TEST MODE - Modified profit target to ₹{new_target_price:.2f}")
            return True

        try:
            rounded_price = round_to_tick_size(new_target_price)

            logger.info(f"📝 Modifying profit target {order_id} to ₹{rounded_price:.2f}")

            response = self.client.modifyorder(
                strategy=f"{strategy_name}_TARGET",
                order_id=order_id,
                symbol=symbol,
                exchange=self.exchange,
                action=self.exit_action,
                price_type="LIMIT",
                product=self.product,
                quantity=quantity,
                price=rounded_price
            )

            if response.get('status') == 'success':
                logger.info(f"✅ Profit target modified successfully")
                return True
            else:
                error_msg = response.get('message', 'Unknown error')

                # Handle order already executed/completed (not an error - target was hit!)
                if 'not a pending order' in error_msg.lower() or 'already executed' in error_msg.lower():
                    logger.info(f"ℹ️  Profit target already executed or completed (cannot modify)")
                    return False  # Cannot modify but not an error condition
                else:
                    logger.error(f"✗ Profit target modification failed: {error_msg}")
                    return False

        except Exception as e:
            logger.error(f"✗ Exception modifying profit target: {e}")
            return False

    def cancel_profit_target_order(self, order_id: str, strategy_name: str = "nifty_sehwag") -> bool:
        """
        Cancel profit target order

        Args:
            order_id: Target order ID
            strategy_name: Strategy identifier

        Returns:
            True if canceled successfully
        """
        if not self.auto_place_orders or not self.place_sl_order_enabled or not order_id:
            return False

        if self.test_mode:
            logger.info(f"🧪 TEST MODE - Canceled profit target")
            return True

        try:
            logger.info(f"🗑️ Canceling profit target order {order_id}")

            response = self.client.cancelorder(
                strategy=f"{strategy_name}_TARGET",
                order_id=order_id
            )

            if response.get('status') == 'success':
                logger.info(f"✅ Profit target canceled successfully")
                return True
            else:
                error_msg = response.get('message', 'Unknown error')

                # Handle already-executed orders
                if 'not a pending order' in error_msg.lower() or 'already executed' in error_msg.lower():
                    logger.info(f"ℹ️  Profit target already executed")
                    return True
                else:
                    logger.error(f"✗ Profit target cancellation failed: {error_msg}")
                    return False

        except Exception as e:
            logger.error(f"✗ Exception canceling profit target: {e}")
            return False
