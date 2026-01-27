"""Logging configuration for the GeoGuessr Research framework."""

import logging
import sys
from typing import Optional

def setup_logging(
        level: int = logging.INFO,
        format_string: Optional[str] = None,
        log_file: Optional[str] = None,
) -> None:
    """
    Configure logging for the application.
    
    Args:
        level: Logging level (default: INFO)
        format_string: Custom format string (optional)
        log_file: Path to log file (optional, logs to stderr if not provided)
    """

    if format_string is None:
        format_string = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    
    handlers: list[logging.Handler] = []

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(logging.Formatter(format_string))
    handlers.append(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter(format_string))
        handlers.append(file_handler)
    
    # Configure root logger
    logging.basicConfig(
        level=level,
        handlers=handlers,
        force=True,
    )
    
    # Set specific loggers
    logging.getLogger("adapters.streetview_js.client").setLevel(level)
    logging.getLogger("core.tools").setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name."""
    return logging.getLogger(name)
