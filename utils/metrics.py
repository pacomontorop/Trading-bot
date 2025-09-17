"""Thread-safe counters and helper utilities for observability metrics."""

from __future__ import annotations

from collections import defaultdict
import threading
from typing import Dict

from utils.cache import stats as cache_stats, reset as cache_reset
from utils.state import StateManager

__all__ = ["inc", "get_all", "cache_metrics"]

_lock = threading.Lock()
_counters: defaultdict[str, int] = defaultdict(int)
_counters.update({k: int(v) for k, v in StateManager.get_metric_counters().items()})


def inc(key: str, n: int = 1) -> None:
    """Increment the counter identified by ``key`` by ``n``."""
    if not key:
        return
    with _lock:
        _counters[key] += int(n)
        StateManager.set_metric_counter(key, _counters[key])


def get_all(reset: bool = False) -> Dict[str, int]:
    """Return a snapshot of all counters.

    Parameters
    ----------
    reset:
        When ``True`` the internal counters are cleared after retrieving the
        snapshot.
    """
    with _lock:
        snapshot = dict(_counters)
        if reset:
            _counters.clear()
            StateManager.replace_metric_counters({})
        else:
            StateManager.replace_metric_counters(snapshot)
        return snapshot


def cache_metrics(reset: bool = False) -> Dict[str, int]:
    """Return cache hit/miss/expired counts."""
    stats = cache_stats()
    metrics = {
        "cache_hits": int(stats.get("hit", 0)),
        "cache_misses": int(stats.get("miss", 0)),
        "cache_expired": int(stats.get("expired", 0)),
    }
    if reset:
        cache_reset()
    return metrics
