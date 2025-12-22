"""
Sehwag Strategy Core - Generic Multi-Index Engine
==================================================
Reusable strategy components that work for any index.

Key Modules:
- strategy: Main orchestrator (SehwagStrategy)
- order_manager: Order execution and tracking
- position_manager: Position and risk management
- market_data: Real-time and historical data
- persistence_manager: Database operations
- logging_manager: Hierarchical logging
- models: Data structures
- database: Database schema and operations

Usage:
    from sehwag.core import SehwagStrategy
    strategy = SehwagStrategy(client, config, websocket_client)
    strategy.run()
"""

from .strategy import SehwagStrategy, is_market_open
from .models import LegPosition, StrategyState, LegSchedule
from .order_manager import OrderManager
from .position_manager import PositionManager
from .market_data import MarketDataManager
from .persistence_manager import SehwagPersistence
from .logging_manager import LoggingManager, get_logging_manager, get_main_logger
from .sehwag_db import init_db, create_session, db_session, SehwagSession

__version__ = "3.0.0"
__author__ = "OpenAlgo Team"

__all__ = [
    # Main strategy
    'SehwagStrategy',
    'is_market_open',

    # Models
    'LegPosition',
    'StrategyState',
    'LegSchedule',

    # Managers
    'OrderManager',
    'PositionManager',
    'MarketDataManager',
    'SehwagPersistence',
    'LoggingManager',

    # Logging helpers
    'get_logging_manager',
    'get_main_logger',

    # DB helpers
    'init_db',
    'create_session',
    'db_session',
    'SehwagSession',
]
