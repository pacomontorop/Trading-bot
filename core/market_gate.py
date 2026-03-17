"""Centralized market open gate with caching and Alpaca/NYSE fallback.

Also provides a VIX-based fear gate: :func:`get_vix_level` returns the current
CBOE VIX reading (cached for 10 minutes) and whether it exceeds the configured
``market.vix_pause_threshold`` in ``config/policy.yaml``.  Set the threshold
to ``0`` (default) to disable the gate entirely.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from utils.logger import log_event

try:  # pragma: no cover - optional dependency used for calendar fallback
    import pandas as _pd
    import pandas_market_calendars as _mcal
except Exception:  # pragma: no cover - missing optional dependency
    _pd = None
    _mcal = None


_CACHE_LOCK = threading.Lock()


@dataclass
class _GateState:
    ts: Optional[datetime] = None
    open: bool = False
    source: str = "alpaca"
    next_open: Optional[datetime] = None
    next_close: Optional[datetime] = None
    last_log: Optional[datetime] = None


_STATE = _GateState()
_CACHE_TTL = 15
_LOG_INTERVAL = 60


def _log_state(now: datetime) -> None:
    """Emit a heartbeat log every ``_LOG_INTERVAL`` seconds."""

    if _STATE.last_log and (now - _STATE.last_log).total_seconds() < _LOG_INTERVAL:
        return

    log_event(
        (
            f"MARKET_GATE is_open_now={_STATE.open} source={_STATE.source} "
            f"now_utc={now.isoformat()} "
            f"next_open={_STATE.next_open.isoformat() if _STATE.next_open else None} "
            f"next_close={_STATE.next_close.isoformat() if _STATE.next_close else None}"
        ),
        event="GATE",
    )
    _STATE.last_log = now


def _fetch_alpaca_state(
    now: datetime, clock: Optional[object] = None
) -> tuple[bool, str, Optional[datetime], Optional[datetime]]:
    if clock is None:
        from broker import alpaca as _alpaca

        clock = _alpaca.api.get_clock()
    open_now = bool(getattr(clock, "is_open", False))

    open_at = getattr(clock, "next_open", None)
    close_at = getattr(clock, "next_close", None)

    def _ensure_dt(value) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:  # pragma: no cover - defensive fallback
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    return open_now, "alpaca", _ensure_dt(open_at), _ensure_dt(close_at)


def _fetch_calendar_state(now: datetime) -> tuple[bool, str, Optional[datetime], Optional[datetime]]:
    if _mcal is None or _pd is None:
        raise RuntimeError("pandas_market_calendars_unavailable")

    cal = _mcal.get_calendar("NYSE")
    start = now - timedelta(days=3)
    end = now + timedelta(days=3)
    schedule = cal.schedule(start_date=start.date(), end_date=end.date())
    if schedule.empty:
        return False, "nyse_cal", None, None

    open_now = False
    next_open: Optional[datetime] = None
    next_close: Optional[datetime] = None

    for _, row in schedule.iterrows():
        open_dt = row["market_open"].tz_convert(timezone.utc)
        close_dt = row["market_close"].tz_convert(timezone.utc)
        if open_dt <= now <= close_dt:
            open_now = True
            next_open = open_dt
            next_close = close_dt
            break
        if now < open_dt:
            next_open = open_dt
            next_close = close_dt
            break
        next_open = open_dt
        next_close = close_dt

    return open_now, "nyse_cal", next_open, next_close


def _update_state(now: datetime, refresh: bool = False) -> None:
    if not refresh and _STATE.ts and (now - _STATE.ts).total_seconds() < _CACHE_TTL:
        _log_state(now)
        return

    try:
        open_now, source, next_open, next_close = _fetch_alpaca_state(now)
    except Exception as exc:  # pragma: no cover - network failure fallback
        try:
            open_now, source, next_open, next_close = _fetch_calendar_state(now)
            log_event(
                (
                    "MARKET_GATE fallback=nyse_cal "
                    f"open={open_now} err=\"{exc}\" now_utc={now.isoformat()}"
                ),
                event="GATE",
            )
        except Exception as cal_exc:  # pragma: no cover - double failure
            log_event(
                (
                    "MARKET_GATE error both sources failed "
                    f"alpaca_err=\"{exc}\" cal_err=\"{cal_exc}\""
                ),
                event="ERROR",
            )
            return

    _STATE.ts = now
    _STATE.open = open_now
    _STATE.source = source
    _STATE.next_open = next_open
    _STATE.next_close = next_close
    _log_state(now)


def is_us_equity_market_open(force_refresh: bool = False) -> bool:
    """Return True if the US equity market is deemed open."""

    now = datetime.now(timezone.utc)
    with _CACHE_LOCK:
        _update_state(now, refresh=force_refresh)
        return _STATE.open


def last_gate_state() -> _GateState:
    """Return a snapshot of the most recent gate state."""

    with _CACHE_LOCK:
        return _GateState(
            ts=_STATE.ts,
            open=_STATE.open,
            source=_STATE.source,
            next_open=_STATE.next_open,
            next_close=_STATE.next_close,
            last_log=_STATE.last_log,
        )


# ---------------------------------------------------------------------------
# VIX fear gate
# ---------------------------------------------------------------------------

_VIX_LOCK = threading.Lock()
_VIX_TTL = 600  # seconds — refresh at most every 10 minutes


@dataclass
class _VixState:
    ts: Optional[datetime] = None
    level: float = 0.0
    elevated: bool = False


_VIX_STATE = _VixState()


def _vix_threshold() -> float:
    """Return the configured VIX pause threshold (0 = disabled)."""
    market_cfg = (getattr(config, "_policy", {}) or {}).get("market", {}) or {}
    return float(market_cfg.get("vix_pause_threshold", 0))


def _fetch_vix() -> Optional[float]:
    """Fetch the latest VIX close from Yahoo Finance. Returns None on failure."""
    try:
        import yfinance as yf  # noqa: PLC0415 — optional, imported lazily

        hist = yf.Ticker("^VIX").history(period="5d")
        if hist.empty:
            return None
        val = float(hist["Close"].dropna().iloc[-1])
        return val if val > 0 else None
    except Exception as exc:  # pragma: no cover - network dependent
        log_event(f"VIX fetch failed err={exc}", event="GATE")
        return None


def get_vix_level(force_refresh: bool = False) -> tuple[float, bool]:
    """Return ``(vix_level, elevated)``.

    *vix_level* is the latest CBOE VIX reading (0.0 if unavailable).
    *elevated* is ``True`` when the threshold is configured (> 0) and the VIX
    exceeds it — signalling that the bot should pause new entries.

    Results are cached for :data:`_VIX_TTL` seconds.
    """
    now = datetime.now(timezone.utc)
    threshold = _vix_threshold()

    with _VIX_LOCK:
        cache_ok = (
            _VIX_STATE.ts is not None
            and (now - _VIX_STATE.ts).total_seconds() < _VIX_TTL
            and not force_refresh
        )
        if not cache_ok:
            fetched = _fetch_vix()
            if fetched is not None:
                # Fresh reading: update state and cache.
                _VIX_STATE.level = fetched
                _VIX_STATE.elevated = threshold > 0 and fetched > threshold
                log_event(
                    f"VIX level={fetched:.1f} threshold={threshold:.0f} "
                    f"elevated={_VIX_STATE.elevated}",
                    event="GATE",
                )
            else:
                # Fetch failed: keep the last known level so an elevated VIX
                # stays elevated rather than silently dropping to 0 and
                # allowing new entries during a market stress period.
                log_event(
                    f"VIX fetch failed — keeping last known level={_VIX_STATE.level:.1f}",
                    event="GATE",
                )
            _VIX_STATE.ts = now

        return _VIX_STATE.level, _VIX_STATE.elevated
