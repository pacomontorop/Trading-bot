"""Lightweight JSON-backed cache with in-memory fallback."""

from __future__ import annotations

import json
import os
import time
from typing import Any


_CACHE: dict[str, dict[str, Any]] = {}
_PERSIST_ENABLED = True
_CACHE_PATH = os.path.join("data", "cache", "quiver_cache.json")


def _load_cache() -> None:
    global _PERSIST_ENABLED
    if not os.path.exists(_CACHE_PATH):
        return
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            _CACHE.update(payload)
    except Exception:
        _PERSIST_ENABLED = False


def _flush_cache() -> None:
    global _PERSIST_ENABLED
    if not _PERSIST_ENABLED:
        return
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump(_CACHE, handle)
    except Exception:
        _PERSIST_ENABLED = False


def get(key: str, ttl: int | float | None = None):
    item = _CACHE.get(key)
    if not item:
        return None
    ts = item.get("ts")
    if ttl is not None and ts is not None and time.time() - float(ts) > ttl:
        _CACHE.pop(key, None)
        _flush_cache()
        return None
    return item.get("data")


def set(key: str, data) -> None:
    _CACHE[key] = {"data": data, "ts": time.time()}
    _flush_cache()


_load_cache()
