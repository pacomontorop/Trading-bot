"""Helpers for bracket order protection and price rounding."""

from __future__ import annotations

import time
from typing import Any, Optional

import config
from core.broker import get_tick_size, round_to_tick


def cancel_all_sells_and_wait(api: Any, symbol: str, open_orders: list) -> bool:
    """Cancel ALL open sell orders for *symbol*, then wait for Alpaca to confirm.

    Alpaca processes cancellations asynchronously (200–2000 ms).  Submitting a
    new order immediately after ``cancel_order()`` often returns
    "insufficient qty available" because the cancelled order still appears open
    server-side.  This helper cancels every sell from *open_orders*, sleeps
    2.5 s, then re-fetches from Alpaca up to 6 times (with 1 s / 2 s / 3 s / 4 s
    / 5 s / 6 s backoff) to confirm the queue is clear before returning.

    The longer timeout (total ~23.5 s vs the old 11.5 s) is needed because
    Alpaca's paper-trading engine can take 10–15 s to reflect a cancellation
    when a stop-limit order is in the ``triggered`` state (stop price was hit,
    limit order is pending fill).

    Returns ``True`` when no open sell orders remain (safe to submit a new one).
    Returns ``False`` when some orders are still active after all retries (caller
    should fall back to suppressing the symbol for a cooldown period).
    """
    from utils.logger import log_event  # local import to avoid circular

    _sells = [
        o for o in (open_orders or [])
        if getattr(o, "symbol", "") == symbol
        and str(getattr(o, "side", "")).lower() == "sell"
    ]

    # If the stale snapshot shows no sells, verify against Alpaca live state
    # before returning True.  A TP/stop placed by the same cycle's TP-renewal
    # pass won't appear in the snapshot but WILL commit the qty in Alpaca,
    # causing the subsequent market-sell to fail with "insufficient qty".
    if not _sells:
        try:
            _live_sells = [
                o for o in api.list_orders(status="open", limit=50)
                if getattr(o, "symbol", "") == symbol
                and str(getattr(o, "side", "")).lower() == "sell"
            ]
        except Exception:
            _live_sells = []
        if not _live_sells:
            return True
        # Found live sells that weren't in the snapshot — cancel them too.
        _sells = _live_sells

    for _o in _sells:
        _oid = getattr(_o, "id", None)
        _ostatus = str(getattr(_o, "status", "")).lower()
        try:
            api.cancel_order(_oid)
        except Exception as _e:
            _estr = str(_e)
            if "429" in _estr or "rate limit" in _estr.lower():
                time.sleep(2.0)
                try:
                    api.cancel_order(_oid)
                except Exception as _e2:
                    log_event(
                        f"symbol={symbol} cancel_order_retry_failed id={_oid} "
                        f"status={_ostatus} err={_e2}",
                        event="ORDER_PROTECTION",
                    )
            else:
                # Log non-rate-limit cancel failures so we know if Alpaca is
                # rejecting the cancel (e.g. order already filled or locked).
                log_event(
                    f"symbol={symbol} cancel_order_failed id={_oid} "
                    f"status={_ostatus} err={_e}",
                    event="ORDER_PROTECTION",
                )

    # Wait for Alpaca to process the cancellations asynchronously.
    # Paper API can take 10-15 s when a stop-limit was just triggered.
    # Increased from 1.5 s to 2.5 s to absorb this extra latency.
    time.sleep(2.5)

    # States that mean Alpaca has accepted the cancel — qty will be released
    # even if the final "cancelled" status hasn't propagated yet.
    _CANCEL_TERMINAL = frozenset(
        ("pending_cancel", "cancelled", "done_for_day", "expired", "replaced")
    )

    for attempt in range(6):  # was range(4); total wait now ~23.5 s vs 11.5 s
        try:
            remaining = [
                o for o in api.list_orders(status="open", limit=50)
                if getattr(o, "symbol", "") == symbol
                and str(getattr(o, "side", "")).lower() == "sell"
            ]
        except Exception as _e:
            if "429" in str(_e) or "rate limit" in str(_e).lower():
                time.sleep(2.0)
            remaining = []
        if not remaining:
            return True
        # Orders already in a terminal-cancel state are confirmed-as-cancelling —
        # Alpaca will not commit their qty for a new order.  Treat as cleared.
        _active = [
            o for o in remaining
            if str(getattr(o, "status", "")).lower() not in _CANCEL_TERMINAL
        ]
        if not _active:
            return True
        # Re-attempt cancelling orders that are still active.
        # Log their current status so we can diagnose future timeouts.
        for _o in _active:
            _oid = getattr(_o, "id", None)
            _ostatus = str(getattr(_o, "status", "")).lower()
            log_event(
                f"symbol={symbol} cancel_retry attempt={attempt + 1} "
                f"id={_oid} status={_ostatus}",
                event="ORDER_PROTECTION",
            )
            try:
                api.cancel_order(_oid)
            except Exception as _e:
                log_event(
                    f"symbol={symbol} cancel_retry_failed attempt={attempt + 1} "
                    f"id={_oid} status={_ostatus} err={_e}",
                    event="ORDER_PROTECTION",
                )
        time.sleep(1.0 * (attempt + 1))  # 1 s → 2 s → 3 s → 4 s → 5 s → 6 s

    return False


def _risk_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("risk", {}) or {}


def _execution_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("execution", {}) or {}


def _tick_for(symbol: str | None, price: float) -> float:
    return get_tick_size(symbol or "", "us_equity", price)


def compute_bracket_prices(
    *,
    symbol: str | None,
    entry_price: float,
    atr: Optional[float],
    risk_cfg: Optional[dict] = None,
    exec_cfg: Optional[dict] = None,
) -> dict:
    """Compute stop-loss/take-profit prices for a bracket order."""

    risk_cfg = risk_cfg or _risk_cfg()
    exec_cfg = exec_cfg or _execution_cfg()

    min_stop_pct = float(risk_cfg.get("min_stop_pct", 0.05))
    atr_k = float(risk_cfg.get("atr_k", 2.0))
    tp_mult = float(exec_cfg.get("take_profit_atr_mult", 3.0))
    min_rr = float(exec_cfg.get("min_rr_ratio", 1.2))
    tick = _tick_for(symbol, entry_price)

    atr_val = float(atr or 0.0)
    if atr_val > 0:
        stop_dist = max(atr_k * atr_val, min_stop_pct * entry_price)
        stop_price_raw = entry_price - stop_dist
        tp_raw = entry_price + tp_mult * atr_val
    else:
        stop_price_raw = entry_price * (1 - min_stop_pct)
        stop_dist = entry_price - stop_price_raw
        tp_raw = entry_price * (1 + min_stop_pct * min_rr)

    stop_price = round_to_tick(stop_price_raw, tick, mode="down")
    take_profit = round_to_tick(tp_raw, tick, mode="up")
    rr_ratio = (take_profit - entry_price) / (entry_price - stop_price) if entry_price > stop_price else 0.0

    return {
        "stop_price": float(stop_price) if stop_price is not None else None,
        "take_profit": float(take_profit) if take_profit is not None else None,
        "stop_dist": float(stop_dist),
        "rr_ratio": float(rr_ratio),
        "tick": float(tick),
    }


def validate_bracket_prices(entry_price: float, stop_price: float, take_profit: float) -> bool:
    """Return True when bracket prices are valid for a long entry."""
    import math as _math

    # Guard against zero, negative, NaN, or inf values in any leg.
    for val in (entry_price, stop_price, take_profit):
        if not (isinstance(val, (int, float)) and _math.isfinite(val) and val > 0):
            return False
    if stop_price >= entry_price:
        return False
    if take_profit <= entry_price:
        return False
    return True


def stop_limit_price(stop_price: float, *, symbol: str | None = None) -> float:
    """Return a stop-limit price for a stop, applying a configured buffer."""

    risk_cfg = _risk_cfg()
    buffer_pct = float(risk_cfg.get("stop_limit_buffer_pct", 0.0))
    if buffer_pct <= 0:
        return stop_price
    tick = _tick_for(symbol, stop_price)
    raw = stop_price * (1 - buffer_pct)
    return float(round_to_tick(raw, tick, mode="down"))


def compute_break_even_stop(
    *,
    symbol: str | None = None,
    entry_price: float,
    initial_stop: float,
    last_price: float,
    break_even_R: float,
    buffer_pct: float,
) -> Optional[float]:
    """Return a new break-even stop price when price has moved by ``break_even_R``."""

    if initial_stop <= 0 or entry_price <= initial_stop:
        return None
    risk_r = entry_price - initial_stop
    if risk_r <= 0:
        return None
    target = entry_price + break_even_R * risk_r
    if last_price < target:
        return None
    raw = entry_price * (1 + buffer_pct)
    tick = _tick_for(symbol, raw)
    return float(round_to_tick(raw, tick, mode="up"))
