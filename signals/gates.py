from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from typing import Tuple, Dict

from broker.alpaca import is_market_open
from signals.filters import is_position_open
from utils.logger import log_event
from utils import metrics
from utils.symbols import detect_asset_class


# ---------------------------------------------------------------------------
# Earnings proximity cache (TTL: 4 hours — earnings dates don't change intraday)
# ---------------------------------------------------------------------------
_EARNINGS_CACHE: dict[str, tuple[bool, float]] = {}
_EARNINGS_CACHE_TTL = 14400  # 4 hours
_EARNINGS_LOCK = threading.Lock()


def _is_earnings_imminent(symbol: str, days_threshold: int = 1) -> bool:
    """Return True if earnings are within days_threshold calendar days.

    Cached per symbol for 4 hours to avoid slowing down the scan loop.
    Fails open (returns False) if data is unavailable — better to trade
    a borderline case than to silently block all signals on a yfinance outage.
    """
    now_ts = time.monotonic()
    with _EARNINGS_LOCK:
        cached = _EARNINGS_CACHE.get(symbol)
        if cached is not None:
            result, ts = cached
            if now_ts - ts < _EARNINGS_CACHE_TTL:
                return result

    # Outside lock: yfinance call (slow path, once per 4h per symbol)
    result = False
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        if cal is not None and not cal.empty and "Earnings Date" in cal.columns:
            today = date.today()
            for ed in cal["Earnings Date"].dropna().tolist()[:2]:
                ed_date = ed.date() if hasattr(ed, "date") else ed
                delta = (ed_date - today).days
                if 0 <= delta <= days_threshold:
                    result = True
                    break
    except Exception:
        result = False  # fail open

    with _EARNINGS_LOCK:
        _EARNINGS_CACHE[symbol] = (result, now_ts)

    return result


def passes_long_gate(symbol: str, data_ctx=None) -> Tuple[bool, Dict]:
    """Safety gate for long trades (market state + tradable equity)."""
    reasons: Dict[str, str] = {}

    if not is_market_open():
        reasons["market"] = "closed"

    asset_class = detect_asset_class(symbol)
    if asset_class != "equity":
        reasons["asset_class"] = asset_class

    if is_position_open(symbol):
        reasons["position"] = "already_open"

    # Earnings proximity guard: skip bot entries within 1 day of earnings.
    # Cowork handles earnings plays with its dedicated scoring system.
    # This prevents the bot from entering a position the day before binary
    # earnings risk — an uninformed entry with poor R:R.
    if _is_earnings_imminent(symbol, days_threshold=1):
        reasons["earnings"] = "imminent_within_1d"

    ok = not reasons
    payload = reasons or {"status": "ok"}
    summary = ", ".join(f"{k}={v}" for k, v in payload.items())
    if ok:
        metrics.inc("gated")
        log_event(
            f"passed gate {summary}",
            event="GATE",
            symbol=symbol,
        )
    else:
        metrics.inc("rejected")
        log_event(
            f"failed gate {summary}",
            event="GATE",
            symbol=symbol,
        )
    return ok, ({} if ok else reasons)
