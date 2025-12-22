"""
Logging Manager for Sehwag Strategy
====================================
Provides hierarchical logging (supports any index):
- Common strategy log file
- Individual leg log files organized by date and strategy name

Industry Standard Structure:
logs/YYYYMMDD/strategy_name/main.log
logs/YYYYMMDD/strategy_name/leg1_*.log
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
import sys
import pytz
from typing import Optional, cast


class LoggingManager:
    """Manages logging for strategy and individual legs"""

    def __init__(self, base_dir: Path = None, strategy_name: str = "sehwag", tz: str | None = "Asia/Kolkata"):
        """
        Initialize logging manager

        Args:
            base_dir: Base directory for logs (defaults to project root, not core/)
            strategy_name: Name of the strategy (e.g., "nifty_sehwag", "sensex_sehwag")
            tz: Timezone name or tzinfo used to compute the date folder (default: 'Asia/Kolkata')
        """
        if base_dir is None:
            # Default to sehwag root (parent of core/), not core/
            base_dir = Path(__file__).parent.parent

        self.base_dir = base_dir
        self.strategy_name = strategy_name

        # Resolve timezone (allow passing tzinfo or timezone name)
        try:
            self.tz = pytz.timezone(tz) if isinstance(tz, str) else tz or pytz.timezone('Asia/Kolkata')
        except Exception:
            # Fallback to UTC if provided tz is invalid
            self.tz = pytz.timezone('Asia/Kolkata')

        # Root logs directory at project level
        self.logs_dir = base_dir / 'logs'
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # Create date-specific directory
        # Use configured timezone when creating the date folder so daily folders match market timezone
        now = datetime.now(self.tz)
        self.date_str = now.strftime('%Y%m%d')
        self.date_logs_dir = self.logs_dir / self.date_str
        self.date_logs_dir.mkdir(parents=True, exist_ok=True)

        # Create strategy-specific directory inside date folder
        self.strategy_logs_dir = self.date_logs_dir / strategy_name
        self.strategy_logs_dir.mkdir(parents=True, exist_ok=True)

        # Track created loggers
        self.leg_loggers = {}

        # Setup main strategy logger
        self.main_logger = self._setup_main_logger()

    def _setup_main_logger(self) -> logging.Logger:
        """Setup main strategy logger"""
        # Main log filename - in strategy-specific folder
        timestamp = datetime.now(self.tz).strftime('%H%M%S')
        log_filename = self.strategy_logs_dir / f"main_{timestamp}.log"

        # Get or create logger - use strategy name for logger name
        logger_name = self.strategy_name.replace('_', '').title()  # e.g., "nifty_sehwag" → "NiftySehwag"
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()

        # File handler
        file_handler = RotatingFileHandler(
            log_filename,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)

        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)

        # Force UTF-8 on Windows console
        if hasattr(sys.stdout, 'reconfigure'):
            try:
                sys.stdout.reconfigure(encoding='utf-8')
            except:
                pass

        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        logger.info(f"📝 Main log file: {log_filename}")
        logger.info(f"📁 Strategy logs: {self.strategy_logs_dir}")
        logger.info(f"🗂️  Structure: logs/{self.date_str}/{self.strategy_name}/")

        return logger

    def get_leg_logger(self, leg_num: int, leg_name: str) -> logging.Logger:
        """
        Get or create a logger for a specific leg

        Args:
            leg_num: Leg number
            leg_name: Leg name

        Returns:
            Logger instance for the leg
        """
        # Check if logger already exists and if leg name matches
        if leg_num in self.leg_loggers:
            existing_logger, existing_name = self.leg_loggers[leg_num]

            # If same leg name, reuse logger
            if existing_name == leg_name:
                existing_logger.info(f"♻️ Reusing existing logger for {leg_name}")
                return existing_logger
            else:
                # Different leg with same number - close old logger first
                self.main_logger.warning(
                    f"⚠️ Leg {leg_num} logger exists for '{existing_name}' but requested for '{leg_name}'"
                )
                self.main_logger.info(f"🔄 Closing old logger and creating new one")
                self._close_leg_logger_internal(leg_num)

        # Create leg log filename with timestamp for uniqueness
        safe_name = leg_name.replace(' ', '_').lower()
        timestamp = datetime.now(self.tz).strftime('%H%M%S')
        log_filename = self.strategy_logs_dir / f"leg{leg_num}_{safe_name}_{timestamp}.log"

        # Create logger with unique name including timestamp
        logger_name = f'{self.strategy_name}.Leg{leg_num}.{timestamp}'
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)

        # Clear ALL existing handlers to prevent cross-contamination
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

        # Prevent propagation to parent (to avoid duplicate logs in main file)
        logger.propagate = False

        # File handler for leg-specific log
        file_handler = RotatingFileHandler(
            log_filename,
            maxBytes=5*1024*1024,  # 5MB
            backupCount=3,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)

        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s - [%(name)s] - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)

        logger.addHandler(file_handler)

        # Cache logger with leg name
        self.leg_loggers[leg_num] = (logger, leg_name)

        # Log to main logger about leg logger creation
        self.main_logger.info(f"📄 Created log file for {leg_name}: {log_filename.name}")

        # Log to leg logger with clear identification
        logger.info(f"=== {leg_name} (Leg #{leg_num}) Log Started ===")
        logger.info(f"Logger Name: {logger_name}")
        logger.info(f"Log file: {log_filename}")

        return logger

    def log_to_both(self, leg_num: int, level: str, message: str):
        """
        Log message to both main log and leg-specific log

        Args:
            leg_num: Leg number
            level: Log level ('debug', 'info', 'warning', 'error')
            message: Log message
        """
        # Log to main logger
        getattr(self.main_logger, level.lower())(message)

        # Log to leg logger if it exists
        if leg_num in self.leg_loggers:
            leg_logger, _ = self.leg_loggers[leg_num]
            getattr(leg_logger, level.lower())(message)

    def _close_leg_logger_internal(self, leg_num: int):
        """
        Internal method to close a leg logger without logging end message

        Args:
            leg_num: Leg number
        """
        if leg_num in self.leg_loggers:
            logger, leg_name = self.leg_loggers[leg_num]

            # Close all handlers
            for handler in logger.handlers[:]:
                try:
                    handler.close()
                except Exception:
                    pass
                logger.removeHandler(handler)

            # Remove from cache
            del self.leg_loggers[leg_num]

            self.main_logger.info(f"🔒 Closed logger for {leg_name} (Leg #{leg_num})")

    def close_leg_logger(self, leg_num: int):
        """
        Close and cleanup a leg logger

        Args:
            leg_num: Leg number
        """
        if leg_num in self.leg_loggers:
            logger, leg_name = self.leg_loggers[leg_num]
            logger.info(f"=== {leg_name} (Leg #{leg_num}) Log Ended ===")

            # Use internal close method
            self._close_leg_logger_internal(leg_num)

    def close_all(self):
        """Close all loggers"""
        # Close all leg loggers
        for leg_num in list(self.leg_loggers.keys()):
            self.close_leg_logger(leg_num)

        # Close main logger
        for handler in self.main_logger.handlers[:]:
            handler.close()
            self.main_logger.removeHandler(handler)

        self.main_logger.info("✓ All loggers closed")


# Global instance
_logging_manager: Optional[LoggingManager] = None


def get_logging_manager(strategy_name: str = "sehwag", base_dir: Path | None = None, tz: str | None = "Asia/Kolkata") -> LoggingManager:
    """Get or create the global logging manager instance.

    Args:
        strategy_name: Strategy name used for the folder (e.g., 'nifty_sehwag')
        base_dir: Optional base directory for logs (defaults to project sehwag root)
        tz: Timezone name used to compute the date folder (default: 'Asia/Kolkata')

    Returns:
        LoggingManager: shared logging manager instance
    """
    global _logging_manager
    if _logging_manager is None:
        # Allow callers to override base_dir/tz for tests or alternative layouts
        _logging_manager = LoggingManager(base_dir=base_dir, strategy_name=strategy_name, tz=tz)
    return _logging_manager


def get_main_logger() -> logging.Logger:
    """Get the main strategy logger"""
    return get_logging_manager().main_logger


def get_leg_logger(leg_num: int, leg_name: str) -> logging.Logger:
    """Get a logger for a specific leg"""
    return get_logging_manager().get_leg_logger(leg_num, leg_name)


def log_to_both(leg_num: int, level: str, message: str):
    """Log to both main and leg-specific log"""
    get_logging_manager().log_to_both(leg_num, level, message)


def close_leg_logger(leg_num: int):
    """Close a specific leg logger"""
    get_logging_manager().close_leg_logger(leg_num)


def close_all_loggers():
    """Close all loggers"""
    global _logging_manager
    if _logging_manager is not None:
        mgr = cast(LoggingManager, _logging_manager)
        mgr.close_all()
        _logging_manager = None

