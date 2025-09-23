"""Tick-size utilities for Alpaca-compliant rounding."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Optional

getcontext().prec = 28


def _to_decimal(value: float | Decimal | str | None) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def equity_tick_for(price: Decimal) -> Decimal:
    return Decimal("0.01") if price >= Decimal("1") else Decimal("0.0001")


def round_to_tick(price: Decimal, tick: Decimal, mode: str) -> Decimal:
    if tick <= 0:
        return price

    epsilon = tick * Decimal("1e-9")

    if mode.upper() == "DOWN":
        quotient, _ = divmod(price, tick)
        return quotient * tick
    if mode.upper() == "UP":
        quotient, remainder = divmod(price, tick)
        if remainder == 0 or remainder.copy_abs() <= epsilon:
            return quotient * tick
        return (quotient + 1) * tick
    if mode.upper() == "NEAREST":
        return (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    raise ValueError(f"Unsupported rounding mode: {mode}")


def round_stop_price(
    symbol: str,
    side: str,
    raw_price: float | Decimal | str | None,
    *,
    asset_class: str = "us_equity",
    tick_override: float | Decimal | str | None = None,
) -> Optional[Decimal]:
    price = _to_decimal(raw_price)
    if price is None:
        return None

    tick = _to_decimal(tick_override)
    if tick is None:
        if asset_class.lower() in {"us_equity", "equity"}:
            tick = equity_tick_for(price)
        else:
            tick = Decimal("0.01")

    mode = "DOWN"
    side_upper = (side or "").upper()
    if side_upper in {"BUY", "COVER", "EXIT_SHORT"}:
        mode = "UP"

    return round_to_tick(price, tick, mode)


def ceil_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return round_to_tick(price, tick, "UP")


def floor_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return round_to_tick(price, tick, "DOWN")
