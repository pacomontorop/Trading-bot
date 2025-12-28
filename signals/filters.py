"""Minimal position checks for long-only equities."""

import os
import time

import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

load_dotenv()
api = tradeapi.REST(
    os.getenv("APCA_API_KEY_ID"),
    os.getenv("APCA_API_SECRET_KEY"),
    "https://paper-api.alpaca.markets",
    api_version="v2",
)

_POSITIONS_CACHE = {"timestamp": 0.0, "data": []}


def get_cached_positions(ttl: int = 60, refresh: bool = False):
    """Return cached positions, refreshing if stale or on demand."""
    now = time.time()
    if refresh or now - _POSITIONS_CACHE["timestamp"] > ttl:
        try:
            _POSITIONS_CACHE["data"] = api.list_positions()
        except Exception:
            _POSITIONS_CACHE["data"] = []
        _POSITIONS_CACHE["timestamp"] = now
    return _POSITIONS_CACHE["data"]


def is_position_open(symbol: str) -> bool:
    """Return True if a position is currently open for ``symbol``."""
    try:
        positions = get_cached_positions()
        return any(p.symbol == symbol for p in positions)
    except Exception:
        return True
