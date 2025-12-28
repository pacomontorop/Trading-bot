"""Shared counters for health heartbeat logging."""

from __future__ import annotations

from collections import Counter
from threading import Lock
from typing import Dict, Literal


_price_lock = Lock()
_scan_lock = Lock()

_price_stats = Counter({"ok": 0, "stale": 0, "failed": 0})
_scan_stats = Counter({"equity": 0})


PriceStatus = Literal["ok", "stale", "failed"]
AssetKind = Literal["equity"]


def record_price(status: PriceStatus) -> None:
    with _price_lock:
        _price_stats[status] += 1


def record_scan(kind: AssetKind, count: int = 1) -> None:
    if count <= 0:
        return
    with _scan_lock:
        _scan_stats[kind] += count


def snapshot(reset: bool = True) -> Dict[str, Dict[str, int]]:
    with _price_lock:
        price_copy = dict(_price_stats)
        if reset:
            for key in list(_price_stats.keys()):
                _price_stats[key] = 0
    with _scan_lock:
        scan_copy = dict(_scan_stats)
        if reset:
            for key in list(_scan_stats.keys()):
                _scan_stats[key] = 0
    return {"prices": price_copy, "scans": scan_copy}
