"""
Sensex Sehwag Strategy - Entry Point (mirrored from Nifty)
=========================================================
Multi-leg breakout strategy for SENSEX trading.

This file is adapted from `nifty_sehwag.py`. It keeps the same runtime
harness and behavior but points to `sensex_sehwag_config.yaml` and
uses `SENSEX` as the underlying by default.
"""

import sys
import os
from pathlib import Path
import yaml
import pytz

# Add project root to path
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from openalgo import api
from core import SehwagStrategy, is_market_open, get_logging_manager, get_main_logger


def load_config():
    """Load strategy configuration"""
    config_file = Path(__file__).parent / 'sensex_sehwag_config.yaml'

    if not config_file.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_file}")

    with open(config_file, 'r') as f:
        config = yaml.safe_load(f)

    return config


def initialize_clients(config, logger):
    """Initialize API and WebSocket clients"""
    api_key = os.getenv(
        "OPENALGO_APIKEY",
        config.get('api', {}).get('key')
    )

    if not api_key or api_key == 'your_openalgo_api_key':
        raise ValueError(
            "Please configure valid API key:\n"
            "  1. Set environment variable: OPENALGO_APIKEY=your_key\n"
            "  2. Or update sensex_sehwag_config.yaml"
        )

    client = api(api_key=api_key)

    websocket_client = None
    if config.get('websocket', {}).get('enabled', False):
        try:
            # Use a relative import so the IDE/static checker can resolve package
            # when running inside the `sehwag` package.
            from ...utils.websocket_ltp_client import create_websocket_client
            websocket_client = create_websocket_client(
                ws_url=os.getenv('WEBSOCKET_URL', config.get('websocket', {}).get('url', 'ws://127.0.0.1:8765')),
                api_key=api_key,
                logger_instance=logger
            )
            websocket_client.start_background()
            logger.info("✓ WebSocket client initialized")
        except Exception as e:
            # Fall back to absolute import attempt for other runtimes
            try:
                from sehwag.utils.websocket_ltp_client import create_websocket_client
                websocket_client = create_websocket_client(
                    ws_url=os.getenv('WEBSOCKET_URL', config.get('websocket', {}).get('url', 'ws://127.0.0.1:8765')),
                    api_key=api_key,
                    logger_instance=logger
                )
                websocket_client.start_background()
                logger.info("✓ WebSocket client initialized (absolute import fallback)")
            except Exception as e2:
                logger.warning(f"WebSocket client initialization failed: {e} / {e2}")
                logger.warning("Will use REST API fallback")

    return client, websocket_client


def run_strategy_main_loop():
    print("\n" + "="*70)
    print("🚀 Sensex Sehwag Strategy (mirrored from Nifty)")
    print("="*70 + "\n")

    logger = None

    try:
        print("📋 Loading configuration...")
        config = load_config()
        print("✓ Configuration loaded")

        underlying = config.get('strategy', {}).get('underlying', 'sehwag')
        strategy_name = f"{underlying.lower()}_sehwag"

        get_logging_manager(strategy_name=strategy_name)
        logger = get_main_logger()

        logger.info("📋 Configuration loaded")

        tz = pytz.timezone('Asia/Kolkata')

        if not is_market_open(tz):
            logger.warning("⚠️  Market is closed. Exiting...")
            logger.info("💡 Tip: Schedule this script to run at market open time (9:15 AM IST)")
            sys.exit(0)

        try:
            from ...core.sehwag_db import init_db
            init_db()
            logger.info("✓ Database initialized")
        except Exception as e:
            logger.warning(f"⚠️  Database initialization failed: {e}")
            logger.warning("Strategy will continue without database persistence")

        logger.info("🔌 Initializing API clients...")
        client, websocket_client = initialize_clients(config, logger)
        logger.info("✓ API clients initialized")

        logger.info("⚙️  Initializing strategy...")
        strategy = SehwagStrategy(client, config, websocket_client)
        logger.info("✓ Strategy initialized")

        strategy.run()

        logger.info("\n✅ Strategy completed successfully\n")

    except KeyboardInterrupt:
        if logger:
            logger.warning("\n⚠️  Strategy interrupted by user\n")
        else:
            print("\n⚠️  Strategy interrupted by user\n")
        sys.exit(0)
    except Exception as e:
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

