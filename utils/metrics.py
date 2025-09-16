from utils.cache import stats as cache_stats, reset as cache_reset


def cache_metrics(reset: bool = False):
    """Return cache hit/miss/expired counts."""
    s = cache_stats()
    metrics = {
        "cache_hits": s.get("hit", 0),
        "cache_misses": s.get("miss", 0),
        "cache_expired": s.get("expired", 0),
    }
    if reset:
        cache_reset()
    return metrics
