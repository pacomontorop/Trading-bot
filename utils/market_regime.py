import time
from datetime import datetime, timedelta
from typing import Literal

_CACHE: dict[str, tuple[dict, float]] = {}


def _cache_get(key: str, ttl: int):
    v = _CACHE.get(key)
    if not v:
        return None
    data, ts = v
    if time.time() - ts > ttl:
        return None
    return data


def _cache_put(key: str, data):
    _CACHE[key] = (data, time.time())


def _get_recent_vix_levels(window_days_list) -> list[float]:
    """Return recent daily VIX levels to cover the requested windows.

    Implementation should query an external data provider such as FMP.
    This stub is provided for tests and can be monkeypatched.
    """
    raise NotImplementedError


def _percentile_rank(values: list[float], last_value: float) -> float:
    if not values:
        return 0.0
    less_eq = sum(1 for v in values if v <= last_value)
    return 100.0 * less_eq / len(values)


def compute_vix_regime(cfg) -> dict:
    """Compute VIX percentiles and classify regime.

    Returns a dict with keys: regime, today, pctiles, composite.
    Results are cached for ``cache_ttl_sec`` seconds.
    """
    mkt = (cfg or {}).get("market", {})
    wins = mkt.get("vix_percentile_windows", [1, 5, 20])
    ttl = int(mkt.get("cache_ttl_sec", 3600))

    cached = _cache_get("vix_regime", ttl)
    if cached:
        return cached

    try:
        levels = _get_recent_vix_levels(wins)
    except Exception:
        levels = []
    today = levels[0] if levels else None

    pctiles = {}
    for w in wins:
        sample = levels[:w] if len(levels) >= w else levels
        pctiles[f"pctl_{w}d"] = _percentile_rank(sample, today) if sample else 0.0

    high_th = float(mkt.get("vix_high_pct", 80))
    elev_th = float(mkt.get("vix_elevated_pct", 60))
    composite = max(pctiles.values()) if pctiles else 0.0
    if composite >= high_th:
        regime = "high_vol"
    elif composite >= elev_th:
        regime = "elevated_vol"
    else:
        regime = "normal"

    data = {
        "regime": regime,
        "today": today,
        "pctiles": pctiles,
        "composite": composite,
    }
    _cache_put("vix_regime", data)
    return data


def exposure_from_regime(cfg, regime: str) -> float:
    mkt = (cfg or {}).get("market", {})
    min_e = float(mkt.get("min_exposure", 0.6))
    max_e = float(mkt.get("max_exposure", 1.0))
    default = float(mkt.get("default_exposure", 0.85))
    if regime == "high_vol":
        return max(min_e, 0.7)
    if regime == "elevated_vol":
        return min(max_e, 0.85)
    return max_e
