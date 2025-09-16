import time
from collections import defaultdict

_store = {}
_metrics = defaultdict(int)

def get(key: str, ttl: int | float | None = None):
    v = _store.get(key)
    if not v:
        _metrics["miss"] += 1
        return None
    data, ts = v
    if ttl is not None and time.time() - ts > ttl:
        _metrics["expired"] += 1
        _store.pop(key, None)
        return None
    _metrics["hit"] += 1
    return data

def set(key: str, data):
    _store[key] = (data, time.time())

def stats():
    return dict(_metrics)

def reset():
    _store.clear()
    _metrics.clear()
