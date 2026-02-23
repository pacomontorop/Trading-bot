"""Order sizing and execution for long-only equities."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict

import config
from broker import alpaca as broker
from broker.account import get_account_equity_safe
from core.order_protection import compute_bracket_prices, stop_limit_price, validate_bracket_prices
from core.safeguards import is_safeguards_active
from utils.logger import log_event


@dataclass
class PositionSizing:
    shares: float
    notional: float
    stop_distance: float
    reason: str | None = None


def _risk_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("risk", {})


def calculate_position_size_risk_based(
    *,
    price: float,
    atr: float | None,
    equity: float | None = None,
) -> PositionSizing:
    """Return a simple risk-based position size for a long entry."""

    risk_cfg = _risk_cfg()
    equity_val = equity if equity is not None else get_account_equity_safe()
    if equity_val <= 0:
        return PositionSizing(0.0, 0.0, 0.0, "invalid_equity")

    risk_pct = float(risk_cfg.get("max_symbol_risk_pct", 0.01))
    max_position_pct = float(risk_cfg.get("max_position_pct", 0.10))
    atr_k = float(risk_cfg.get("atr_k", 2.0))
    min_stop_pct = float(risk_cfg.get("min_stop_pct", 0.05))
    allow_fractional = bool(risk_cfg.get("allow_fractional", True))

    stop_distance = max((atr or 0.0) * atr_k, price * min_stop_pct)
    if stop_distance <= 0:
        return PositionSizing(0.0, 0.0, 0.0, "invalid_stop_distance")

    risk_budget = equity_val * risk_pct
    shares = risk_budget / stop_distance
    if shares <= 0:
        return PositionSizing(0.0, 0.0, stop_distance, "risk_budget_too_small")

    max_notional = equity_val * max_position_pct
    notional = shares * price
    if notional > max_notional:
        shares = max_notional / price
        notional = shares * price

    if not allow_fractional:
        shares = int(shares)
        notional = shares * price

    if shares <= 0 or notional <= 0:
        return PositionSizing(0.0, 0.0, stop_distance, "size_floor_zero")

    return PositionSizing(float(shares), float(notional), float(stop_distance), None)


def _execution_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("execution", {})


def place_long_order(plan: dict, *, dry_run: bool = False) -> bool:
    """Place a long-only order using Alpaca."""

    exec_cfg = _execution_cfg()
    risk_cfg = _risk_cfg()
    symbol = plan.get("symbol")
    qty = float(plan.get("qty") or 0)
    price = float(plan.get("price") or 0)
    atr = plan.get("atr")
    use_bracket = bool(plan.get("use_bracket", False))
    time_in_force = plan.get("time_in_force", exec_cfg.get("time_in_force", "day"))

    if qty <= 0 or not symbol:
        log_event(f"ORDER {symbol}: rejected reason=zero_qty", event="ORDER")
        return False

    if not is_safeguards_active():
        log_event(f"ORDER {symbol}: rejected reason=safeguards_inactive", event="ORDER")
        return False

    if not use_bracket:
        log_event(f"ORDER {symbol}: rejected reason=unprotected_order", event="ORDER")
        return False

    client_order_id = f"LONG.{symbol}.{int(price * 100)}.{int(time.time())}"

    if dry_run:
        log_event(
            (
                f"DRY_RUN ORDER {symbol}: qty={qty:.2f} "
                f"price={price:.2f}"
            ),
            event="ORDER",
        )
        return True

    bracket = compute_bracket_prices(
        symbol=symbol,
        entry_price=price,
        atr=atr,
        risk_cfg=risk_cfg,
        exec_cfg=exec_cfg,
    )
    stop_loss = bracket["stop_price"]
    take_profit = bracket["take_profit"]
    tick = bracket["tick"]

    if not validate_bracket_prices(price, stop_loss, take_profit):
        log_event(f"ORDER {symbol}: rejected reason=invalid_bracket_prices", event="ORDER")
        return False

    stop_payload = {"stop_price": stop_loss}
    stop_limit = stop_limit_price(stop_loss, symbol=symbol)
    if stop_limit and stop_limit < stop_loss:
        stop_payload["limit_price"] = stop_limit

    log_event(
        (
            "ORDER_SUBMIT "
            f"symbol={symbol} side=buy qty={qty:.2f} order_class=bracket "
            f"entry={price:.2f} atr={float(atr or 0.0):.4f} sl={stop_loss:.4f} "
            f"tp={take_profit:.4f} tick={tick}"
        ),
        event="ORDER",
    )
    try:  # pragma: no cover - network
        broker.api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="market",
            time_in_force=time_in_force,
            order_class="bracket",
            take_profit={"limit_price": take_profit},
            stop_loss=stop_payload,
            client_order_id=client_order_id,
        )
        return True
    except Exception as exc:  # pragma: no cover - network
        log_event(f"ORDER {symbol}: failed {exc}", event="ORDER")
        return False
