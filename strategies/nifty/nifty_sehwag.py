"""
Nifty Sehwag Strategy - Entry Point (Clean & Modular v3.0)
===========================================================
Multi-leg breakout strategy for NIFTY trading.

Features:
✓ Fetch high/low and expiry ONCE at startup (no loops)
✓ Check breakout condition at each leg's entry time
✓ Each leg runs in its own thread
✓ WebSocket-based real-time monitoring
✓ Database persistence for positions and events

Configuration: nifty_sehwag_config.yaml
Version: 3.0.0 (Clean Refactor)
"""

import sys
import os
from pathlib import Path
import yaml
import pytz

# Add project root to path
# Current file: .../sehwag/strategies/nifty/nifty_sehwag.py
# Need to add: .../my-strategies (parent.parent.parent)
# So that `import sehwag.core` works correctly
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

# Ensure package imports find `sehwag` as a package
from openalgo import api
# Import strategy and logging helpers from the package
from core import SehwagStrategy, is_market_open, get_logging_manager, get_main_logger


def load_config():
    """Load strategy configuration"""
    config_file = Path(__file__).parent / 'nifty_sehwag_config.yaml'

    if not config_file.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_file}")

    with open(config_file, 'r') as f:
        config = yaml.safe_load(f)

    return config


def initialize_clients(config, logger):
    """Initialize API and WebSocket clients"""
    # Get API key from environment variable or config (priority: env var > config)
    api_key = os.getenv(
        "OPENALGO_APIKEY",
        config.get('api', {}).get('key')
    )

    if not api_key or api_key == 'your_openalgo_api_key':
        raise ValueError(
            "Please configure valid API key:\n"
            "  1. Set environment variable: OPENALGO_APIKEY=your_key\n"
            "  2. Or update nifty_sehwag_config.yaml"
        )

    # Initialize API client
    client = api(api_key=api_key)

    # Initialize WebSocket client if enabled
    websocket_client = None
    if config.get('websocket', {}).get('enabled', False):
        try:
            from sehwag.utils.websocket_ltp_client import create_websocket_client
            websocket_client = create_websocket_client(
                ws_url=os.getenv('WEBSOCKET_URL', config.get('websocket', {}).get('url', 'ws://127.0.0.1:8765')),
                api_key=api_key,
                logger_instance=logger
            )
            websocket_client.start_background()
            logger.info("✓ WebSocket client initialized")
        except Exception as e:
            logger.warning(f"WebSocket client initialization failed: {e}")
            logger.warning("Will use REST API fallback")

    return client, websocket_client


def run_strategy_main_loop():
    """
    Main strategy execution (designed to be scheduled externally)

    Use a scheduler like cron/Task Scheduler to run at market open time:
    - Linux/Mac: crontab -e -> "15 9 * * 1-5 /path/to/python /path/to/nifty_sehwag.py"
    - Windows Task Scheduler: Run at 9:15 AM on weekdays
    - APScheduler: See scheduler example in docs
    """
    # Note: We initialize the logging manager AFTER loading configuration so
    # the logs directory can include the underlying index (e.g., "nifty_sehwag").
    # Use print() before logger exists to provide startup feedback.
    print("\n" + "="*70)
    print("🚀 Nifty Sehwag Strategy v3.0 (Clean & Modular)")
    print("="*70 + "\n")

    try:
        # Load configuration
        print("📋 Loading configuration...")
        config = load_config()
        print("✓ Configuration loaded")

        # Derive strategy_name from config's underlying (e.g., "NIFTY" -> "nifty_sehwag")
        underlying = config.get('strategy', {}).get('underlying', 'sehwag')
        strategy_name = f"{underlying.lower()}_sehwag"

        # Initialize logging manager with derived strategy name
        get_logging_manager(strategy_name=strategy_name)
        logger = get_main_logger()

        logger.info("📋 Configuration loaded")

        # Get timezone
        tz = pytz.timezone('Asia/Kolkata')

        # Quick market check - exit if closed
        if not is_market_open(tz):
            logger.warning("⚠️  Market is closed. Exiting...")
            logger.info("💡 Tip: Schedule this script to run at market open time (9:15 AM IST)")
            sys.exit(0)

        # Initialize database
        try:
            from ...core.sehwag_db import init_db
            init_db()
            logger.info("✓ Database initialized")
        except Exception as e:
            logger.warning(f"⚠️  Database initialization failed: {e}")
            logger.warning("Strategy will continue without database persistence")

        # Initialize clients
        logger.info("🔌 Initializing API clients...")
        client, websocket_client = initialize_clients(config, logger)
        logger.info("✓ API clients initialized")

        # Initialize strategy
        logger.info("⚙️  Initializing strategy...")
        strategy = SehwagStrategy(client, config, websocket_client)
        logger.info("✓ Strategy initialized")

        # Run strategy
        strategy.run()

        logger.info("\n✅ Strategy completed successfully\n")

    except KeyboardInterrupt:
        if logger:
            logger.warning("\n⚠️  Strategy interrupted by user\n")
        else:
            print("\n⚠️  Strategy interrupted by user\n")
        sys.exit(0)
    except Exception as e:
        # If logger isn't initialized for some reason, fall back to print
        if logger:
            try:
                logger.error(f"\n❌ Strategy failed: {e}")
            except Exception:
                print(f"\n❌ Strategy failed: {e}")
        else:
            print(f"\n❌ Strategy failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run_strategy_main_loop()
