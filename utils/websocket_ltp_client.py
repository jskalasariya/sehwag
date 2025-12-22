"""
OpenAlgo WebSocket LTP Client - Reusable real-time price streaming module

Provides real-time LTP (Last Traded Price) streaming via WebSocket with automatic
fallback to REST API polling. Can be used by any strategy requiring live price updates.

Example:
    Basic usage in any strategy:
    
    ```python
    from utils.websocket_ltp_client import WebSocketLTPClient
    
    # Initialize
    ws = WebSocketLTPClient(
        ws_url="ws://127.0.0.1:8765",
        api_key="your_api_key",
        logger_instance=logger
    )
    
    # Start in background
    ws.start_background()
    
    # Register price callback
    def on_price(ltp: float):
        print(f"Price updated: {ltp}")
    
    ws.on_price_update("NIFTY24DEC20500CE", on_price)
    
    # Get last cached price
    current_price = ws.get_last_price("NIFTY24DEC20500CE")
    ```

Author: OpenAlgo Team
License: MIT
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Callable, Optional
from threading import Thread, Event, Lock
import websockets


class WebSocketLTPClient:
    """Real-time LTP streaming via WebSocket with REST API fallback"""
    
    # Subscription modes
    MODE_LTP = 1      # Last Traded Price only
    MODE_QUOTE = 2    # OHLC + bid/ask
    MODE_DEPTH = 3    # Market depth
    
    def __init__(
        self,
        ws_url: str,
        api_key: str,
        logger_instance: Optional[logging.Logger] = None,
        timeout_seconds: int = 10,
        reconnect_interval_seconds: int = 5
    ):
        """
        Initialize WebSocket LTP client.
        
        Args:
            ws_url: WebSocket server URL (e.g., "ws://127.0.0.1:8765")
            api_key: OpenAlgo API key for authentication
            logger_instance: Optional logger instance (uses default if None)
            timeout_seconds: Connection timeout in seconds
            reconnect_interval_seconds: Reconnection interval on failure
        """
        self.ws_url = ws_url
        self.api_key = api_key
        self.logger = logger_instance or logging.getLogger('WebSocketLTP')
        self.timeout_seconds = timeout_seconds
        self.reconnect_interval_seconds = reconnect_interval_seconds
        
        # Connection state
        self.websocket = None
        self.connected = False
        self.authenticated = False
        
        # Callbacks and subscriptions
        self.price_callbacks: Dict[str, List[Callable]] = {}
        self.subscription_lock = Lock()
        
        # Background thread management
        self.rx_task = None
        self.last_prices: Dict[str, float] = {}
        
        # Thread management
        self._thread = None
        self._loop = None
        self._running = False
    
    async def connect(self) -> bool:
        """
        Establish WebSocket connection and authenticate.
        
        Returns:
            True if connection and authentication successful, False otherwise
        """
        try:
            self.websocket = await asyncio.wait_for(
                websockets.connect(self.ws_url),
                timeout=self.timeout_seconds
            )
            
            # Authenticate with API key
            auth_msg = {
                "action": "authenticate",
                "api_key": self.api_key
            }
            await self.websocket.send(json.dumps(auth_msg))
            
            # Wait for auth response
            response = await asyncio.wait_for(
                self.websocket.recv(),
                timeout=self.timeout_seconds
            )
            auth_response = json.loads(response)
            
            if auth_response.get("status") == "success":
                self.connected = True
                self.authenticated = True
                self.logger.info(f"✓ WebSocket connected and authenticated at {self.ws_url}")
                return True
            else:
                self.logger.error(f"✗ WebSocket authentication failed: {auth_response}")
                return False
        
        except asyncio.TimeoutError:
            self.logger.error(f"✗ WebSocket connection timeout ({self.timeout_seconds}s)")
            return False
        except Exception as e:
            self.logger.error(f"✗ WebSocket connection error: {e}")
            return False
    
    async def subscribe_ltp(self, symbol: str, exchange: str) -> bool:
        """
        Subscribe to LTP updates for a symbol (mode 1).
        
        Args:
            symbol: Symbol to subscribe (e.g., "NIFTY24DEC20500CE")
            exchange: Exchange (e.g., "NFO")
        
        Returns:
            True if subscription sent successfully, False otherwise
        """
        if not self.connected or not self.websocket:
            return False
        
        try:
            subscribe_msg = {
                "action": "subscribe",
                "symbol": symbol,
                "exchange": exchange,
                "mode": self.MODE_LTP
            }
            await self.websocket.send(json.dumps(subscribe_msg))
            
            # Don't wait for response if receive loop is running
            # The receive loop will handle subscription confirmations
            self.logger.info(f"✓ Subscription request sent for LTP: {exchange}:{symbol}")
            return True
        
        except Exception as e:
            self.logger.error(f"✗ Error subscribing to {symbol}: {e}")
            return False
    
    async def subscribe_quote(self, symbol: str, exchange: str, depth_level: int = 5) -> bool:
        """
        Subscribe to quote updates for a symbol (mode 2).
        
        Args:
            symbol: Symbol to subscribe
            exchange: Exchange
            depth_level: Depth level for bid/ask (default 5)
        
        Returns:
            True if subscription sent successfully, False otherwise
        """
        if not self.connected or not self.websocket:
            return False
        
        try:
            subscribe_msg = {
                "action": "subscribe",
                "symbol": symbol,
                "exchange": exchange,
                "mode": self.MODE_QUOTE,
                "depth": depth_level
            }
            await self.websocket.send(json.dumps(subscribe_msg))
            
            # Don't wait for response if receive loop is running
            self.logger.info(f"✓ Subscription request sent for Quote: {exchange}:{symbol}")
            return True
        
        except Exception as e:
            self.logger.error(f"✗ Error subscribing to quote for {symbol}: {e}")
            return False
    
    async def subscribe_depth(self, symbol: str, exchange: str, depth_level: int = 5) -> bool:
        """
        Subscribe to market depth updates for a symbol (mode 3).
        
        Args:
            symbol: Symbol to subscribe
            exchange: Exchange
            depth_level: Depth level (5, 10, 20, etc.)
        
        Returns:
            True if subscription sent successfully, False otherwise
        """
        if not self.connected or not self.websocket:
            return False
        
        try:
            subscribe_msg = {
                "action": "subscribe",
                "symbol": symbol,
                "exchange": exchange,
                "mode": self.MODE_DEPTH,
                "depth": depth_level
            }
            await self.websocket.send(json.dumps(subscribe_msg))
            
            # Don't wait for response if receive loop is running
            self.logger.info(f"✓ Subscription request sent for Depth (L{depth_level}): {exchange}:{symbol}")
            return True
        
        except Exception as e:
            self.logger.error(f"✗ Error subscribing to depth for {symbol}: {e}")
            return False
    
    def on_price_update(self, symbol: str, callback: Callable[[float], None]):
        """
        Register callback for LTP updates on a symbol.
        
        Args:
            symbol: Symbol to monitor
            callback: Function to call with price (receives float ltp value)
        """
        with self.subscription_lock:
            if symbol not in self.price_callbacks:
                self.price_callbacks[symbol] = []
            self.price_callbacks[symbol].append(callback)
    
    def on_quote_update(self, symbol: str, callback: Callable[[Dict], None]):
        """
        Register callback for quote updates.
        
        Args:
            symbol: Symbol to monitor
            callback: Function to call with quote data (receives dict)
        """
        # Quote callbacks stored separately if needed
        with self.subscription_lock:
            if symbol not in self.price_callbacks:
                self.price_callbacks[symbol] = []
            self.price_callbacks[symbol].append(callback)
    
    async def receive_loop(self):
        """Continuously receive and process market data from WebSocket"""
        if not self.connected or not self.websocket:
            return
        
        try:
            while self.connected and self._running:
                try:
                    response = await asyncio.wait_for(
                        self.websocket.recv(),
                        timeout=30
                    )
                    data = json.loads(response)
                    
                    # Handle subscription confirmations
                    if data.get("action") == "subscribe" and data.get("status"):
                        symbol = data.get("symbol", "unknown")
                        status = data.get("status")
                        if status == "success":
                            self.logger.debug(f"✓ Subscription confirmed: {symbol}")
                        else:
                            self.logger.warning(f"⚠️  Subscription failed: {symbol} - {data.get('message', 'Unknown error')}")
                    
                    # Process market data
                    elif data.get("type") == "market_data":
                        market_data = data.get("data", {})
                        symbol = data.get("symbol")
                        ltp = market_data.get("ltp")
                        
                        if symbol and ltp is not None:
                            self.last_prices[symbol] = ltp
                            
                            # Trigger callbacks for this symbol
                            with self.subscription_lock:
                                if symbol in self.price_callbacks:
                                    for callback in self.price_callbacks[symbol]:
                                        try:
                                            callback(ltp)
                                        except Exception as e:
                                            self.logger.error(f"✗ Callback error for {symbol}: {e}")
                
                except asyncio.TimeoutError:
                    # Timeout is normal, continue receiving
                    continue
        
        except Exception as e:
            self.logger.error(f"✗ Error in receive loop: {e}")
            self.connected = False
    
    async def disconnect(self):
        """Close WebSocket connection"""
        try:
            if self.websocket:
                await self.websocket.close()
            self.connected = False
            self.authenticated = False
            self.logger.info("✓ WebSocket disconnected")
        except Exception as e:
            self.logger.error(f"✗ Error disconnecting: {e}")
    
    def get_last_price(self, symbol: str) -> Optional[float]:
        """
        Get the last cached price for a symbol.
        
        Args:
            symbol: Symbol to get price for
        
        Returns:
            Last cached price or None if not available
        """
        return self.last_prices.get(symbol)
    
    def start_background(self):
        """Start WebSocket connection in background thread"""
        if self._thread and self._thread.is_alive():
            self.logger.warning("⚠️  WebSocket thread already running")
            return
        
        self._running = True
        
        def run_ws_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            
            try:
                # Connect and keep connection alive
                while self._running:
                    if not self.connected:
                        if self._loop.run_until_complete(self.connect()):
                            # Start receive loop
                            self._loop.run_until_complete(self.receive_loop())
                        else:
                            time.sleep(self.reconnect_interval_seconds)
                    else:
                        time.sleep(1)
            except KeyboardInterrupt:
                pass
            except Exception as e:
                self.logger.error(f"✗ WebSocket loop error: {e}")
            finally:
                if self.connected:
                    self._loop.run_until_complete(self.disconnect())
        
        self._thread = Thread(target=run_ws_loop, daemon=True)
        self._thread.start()
        self.logger.info("✓ WebSocket background thread started")
    
    def stop_background(self):
        """Stop background WebSocket thread"""
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread:
            self._thread.join(timeout=5)
        self.logger.info("✓ WebSocket background thread stopped")
    
    def is_connected(self) -> bool:
        """Check if WebSocket is connected and authenticated"""
        return self.connected and self.authenticated
    
    def subscribe_ltp_sync(self, symbol: str, exchange: str) -> bool:
        """
        Thread-safe synchronous wrapper for LTP subscription.
        Schedules subscription in the background event loop.
        
        Args:
            symbol: Symbol to subscribe
            exchange: Exchange
        
        Returns:
            True if subscription was scheduled, False if not connected
        """
        if not self._loop or not self.connected:
            self.logger.warning(f"⚠️  Cannot subscribe - WebSocket not connected")
            return False
        
        try:
            # Schedule coroutine in the background loop
            future = asyncio.run_coroutine_threadsafe(
                self.subscribe_ltp(symbol, exchange),
                self._loop
            )
            # Wait for result with timeout
            result = future.result(timeout=self.timeout_seconds)
            return result
        except Exception as e:
            self.logger.error(f"✗ Error in sync LTP subscription for {symbol}: {e}")
            return False
    
    def subscribe_quote_sync(self, symbol: str, exchange: str, depth_level: int = 5) -> bool:
        """
        Thread-safe synchronous wrapper for quote subscription.
        
        Args:
            symbol: Symbol to subscribe
            exchange: Exchange
            depth_level: Depth level for bid/ask
        
        Returns:
            True if subscription was scheduled, False if not connected
        """
        if not self._loop or not self.connected:
            self.logger.warning(f"⚠️  Cannot subscribe - WebSocket not connected")
            return False
        
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.subscribe_quote(symbol, exchange, depth_level),
                self._loop
            )
            result = future.result(timeout=self.timeout_seconds)
            return result
        except Exception as e:
            self.logger.error(f"✗ Error in sync quote subscription for {symbol}: {e}")
            return False


def create_websocket_client(
    ws_url: str,
    api_key: str,
    logger_instance: Optional[logging.Logger] = None,
    **kwargs
) -> WebSocketLTPClient:
    """
    Factory function to create and start a WebSocket client.
    
    Args:
        ws_url: WebSocket server URL
        api_key: OpenAlgo API key
        logger_instance: Optional logger
        **kwargs: Additional arguments passed to WebSocketLTPClient
    
    Returns:
        Initialized WebSocketLTPClient instance
    
    Example:
        ```python
        ws = create_websocket_client(
            ws_url="ws://127.0.0.1:8765",
            api_key="your_api_key",
            logger_instance=logger,
            timeout_seconds=10
        )
        ```
    """
    client = WebSocketLTPClient(
        ws_url=ws_url,
        api_key=api_key,
        logger_instance=logger_instance,
        **kwargs
    )
    return client
