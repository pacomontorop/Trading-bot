import time
from datetime import datetime, timedelta, timezone
from signals.fmp_utils import search_stock_news

# Simple in-memory cache
_CACHE: dict[str, tuple[object, float]] = {}

def _cache_get(key: str, ttl: int = 900):
    v = _CACHE.get(key)
    if not v:
        return None
    data, ts = v
    if time.time() - ts > ttl:
        return None
    return data


def _cache_put(key: str, data):
    _CACHE[key] = (data, time.time())


def next_session_close_utc() -> datetime:
    """Return the next session close time in UTC.

    Placeholder implementation uses 20:00 UTC (~16:00 ET) for regular NYSE
    close without accounting for holidays or early closes.
    """
    today = datetime.utcnow().date()
    return datetime(today.year, today.month, today.day, 20, 0, 0, tzinfo=timezone.utc)


def minutes_to_close(now_utc: datetime | None = None) -> int:
    now = now_utc or datetime.utcnow().replace(tzinfo=timezone.utc)
    close = next_session_close_utc()
    return max(0, int((close - now).total_seconds() // 60))


def earnings_within(symbol: str, days: int) -> bool:
    """Heuristic detection of earnings/guidance/dividend events within ±days.

    This uses basic FMP news search for the symbol looking for keywords. If the
    FMP plan provides an earnings or dividends calendar endpoint, that could be
    integrated here and preferred over the news heuristic.
    """
    key = f"earnings:{symbol}:{days}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    from_date = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    to_date = (datetime.utcnow() + timedelta(days=days)).date().isoformat()

    items = search_stock_news(symbols=symbol, from_date=from_date, to_date=to_date, limit=20) or []
    kws = ("earnings", "guidance", "EPS", "outlook", "dividend", "payout")
    flag = False
    for it in items:
        title = (it.get("title") or "").lower()
        if any(k in title for k in kws):
            flag = True
            break

    _cache_put(key, flag)
    return flag
