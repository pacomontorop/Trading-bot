"""Resilient helpers for retrieving account equity with caching."""

from __future__ import annotations

import time

from broker.alpaca import api
from utils.logger import log_event

_last_equity: float | None = None
_last_equity_ts: float = 0.0


def _fetch_account_equity() -> float | None:
    try:  # pragma: no cover - network call
        account = api.get_account()
        value = getattr(account, "equity", None)
        if value in (None, ""):
            return None
        equity = float(value)
        return equity if equity > 0 else None
    except Exception as exc:  # pragma: no cover - defensive
        log_event(f"ERROR EQUITY: {exc}")
        return None


def get_account_equity_safe(max_age_sec: float = 86400.0) -> float:
    """Return last known positive equity, retrying the broker if possible."""

    global _last_equity, _last_equity_ts

    equity = _fetch_account_equity()
    if equity is not None and equity > 0:
        _last_equity = equity
        _last_equity_ts = time.time()
        return equity

    if _last_equity is not None and (time.time() - _last_equity_ts) < max_age_sec:
        return _last_equity

    return 0.0
