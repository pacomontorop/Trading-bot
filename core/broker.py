"""Broker-related helpers for price precision handling."""

from __future__ import annotations

import math
from typing import Optional

import config

TICK_DEFAULTS = {
    "equity_ge_1": 0.01,
    "equity_lt_1": 0.0001,
    "etf": 0.01,
    "option": 0.01,
    "crypto": 0.01,
}


def _policy_ticks() -> dict[str, float]:
    risk_cfg = getattr(config, "_policy", {}).get("risk", {}) if getattr(config, "_policy", None) else {}
    return {
        "equity_ge_1": float(risk_cfg.get("min_tick_equity_ge_1", TICK_DEFAULTS["equity_ge_1"])),
        "equity_lt_1": float(risk_cfg.get("min_tick_equity_lt_1", TICK_DEFAULTS["equity_lt_1"])),
        "etf": float(risk_cfg.get("min_tick_etf", TICK_DEFAULTS["etf"])),
        "option": float(risk_cfg.get("min_tick_option", TICK_DEFAULTS["option"])),
        "crypto": float(risk_cfg.get("min_tick_crypto", TICK_DEFAULTS["crypto"])),
    }


def get_tick_size(symbol: str, asset_class: Optional[str], price: Optional[float]) -> float:
    """Return the tick size to use for ``symbol`` at ``price``."""

    ticks = _policy_ticks()
    if (asset_class or "").lower() == "crypto":
        return ticks["crypto"]
    if price is None:
        return ticks["equity_ge_1"]
    if price < 1.0:
        return ticks["equity_lt_1"]
    return ticks["equity_ge_1"]


def round_to_tick(price: Optional[float], tick: Optional[float], mode: str = "nearest") -> Optional[float]:
    """Round ``price`` to the nearest valid ``tick`` according to ``mode``."""

    if price is None or tick is None or tick <= 0:
        return price
    if mode == "down":
        return math.floor(price / tick) * tick
    if mode == "up":
        return math.ceil(price / tick) * tick
    return round(price / tick) * tick

