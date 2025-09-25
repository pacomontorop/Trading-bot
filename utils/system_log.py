"""Shared logging configuration for the trading bot."""

from __future__ import annotations

import logging
from typing import Optional


_DEFAULT_FMT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a configured :class:`logging.Logger` instance.

    The logger writes to stdout with a compact timestamped formatter.  The
    configuration is applied only once per logger to avoid duplicate handlers.
    """

    logger = logging.getLogger(name if name else "trading")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt=_DEFAULT_FMT,
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
