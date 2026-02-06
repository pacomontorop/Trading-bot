"""Centralized market open gate with caching and Alpaca/NYSE fallback."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

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
