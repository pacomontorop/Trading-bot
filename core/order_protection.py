"""Helpers for bracket order protection and price rounding."""

from __future__ import annotations

from typing import Optional

import config
from core.broker import get_tick_size, round_to_tick


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

    if stop_price <= 0 or take_profit <= 0:
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
