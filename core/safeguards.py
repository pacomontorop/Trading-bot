"""Post-entry safeguards: break-even and trailing protection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import config
from broker import alpaca as broker
from core.order_protection import compute_bracket_prices, compute_break_even_stop, stop_limit_price
from utils.logger import log_event

_TTL_LOGGED = False


def _risk_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("risk", {}) or {}


def _execution_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("execution", {}) or {}


def _safeguards_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("safeguards", {}) or {}


def _parse_started_at(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).astimezone(timezone.utc)
    except Exception:
        return None


def is_safeguards_active(now: Optional[datetime] = None) -> bool:
    """Return True when safeguard TTL is active."""

    global _TTL_LOGGED
    cfg = _safeguards_cfg()
    if not cfg or not bool(cfg.get("enabled", False)):
        return False
    ttl_days = float(cfg.get("ttl_days", 0))
    started_at = _parse_started_at(cfg.get("started_at_utc"))
    if not ttl_days or not started_at:
        return True
    now = now or datetime.now(timezone.utc)
    expires_at = started_at + timedelta(days=ttl_days)
    if now >= expires_at:
        if not _TTL_LOGGED:
            log_event(
                (
                    "SAFEGUARDS ttl_expired "
                    f"started_at={started_at.isoformat()} ttl_days={ttl_days} now={now.isoformat()}"
                ),
                event="RISK",
            )
            _TTL_LOGGED = True
        return False
    return True


def _iter_orders(orders: Iterable) -> Iterable:
    for order in orders or []:
        yield order
        legs = getattr(order, "legs", None)
        if legs:
            for leg in legs:
                yield leg


def _extract_stop(order) -> Optional[float]:
    stop_price = getattr(order, "stop_price", None)
    if stop_price is None:
        return None
    try:
        return float(stop_price)
    except Exception:
        return None


def _extract_trail(order) -> tuple[Optional[float], Optional[float]]:
    trail_price = getattr(order, "trail_price", None)
    trail_percent = getattr(order, "trail_percent", None)
    try:
        trail_price_val = float(trail_price) if trail_price is not None else None
    except Exception:
        trail_price_val = None
    try:
        trail_percent_val = float(trail_percent) if trail_percent is not None else None
    except Exception:
        trail_percent_val = None
    return trail_price_val, trail_percent_val


def _find_protection_orders(symbol: str, orders: Iterable) -> tuple[Optional[object], Optional[object]]:
    stop_order = None
    trailing_order = None
    for order in _iter_orders(orders):
        if getattr(order, "symbol", "") != symbol:
            continue
        if getattr(order, "side", "").lower() != "sell":
            continue
        order_type = str(getattr(order, "type", "") or getattr(order, "order_type", "")).lower()
        if order_type in {"stop", "stop_limit"}:
            stop_order = order
        if order_type == "trailing_stop":
            trailing_order = order
    return stop_order, trailing_order


def _should_skip_for_pending(symbol: str, orders: Iterable) -> bool:
    risk_cfg = _risk_cfg()
    if not bool(risk_cfg.get("skip_if_order_pending", True)):
        return False
    for order in orders or []:
        if getattr(order, "symbol", "") == symbol:
            return True
    return False


def run_safeguards() -> None:
    if not is_safeguards_active():
        return

    exec_cfg = _execution_cfg()
    risk_cfg = _risk_cfg()
    safeguards_cfg = _safeguards_cfg()

    positions = broker.list_positions()
    orders = broker.list_open_orders_today()

    break_even_r = float(safeguards_cfg.get("break_even_R", 1.0))
    break_even_buffer = float(safeguards_cfg.get("break_even_buffer_pct", 0.001))
    trailing_enable = bool(safeguards_cfg.get("trailing_enable", True))
    trailing_mult = float(exec_cfg.get("trailing_stop_atr_mult", 0.0) or 0.0)

    for pos in positions:
        try:
            symbol = getattr(pos, "symbol", "")
            qty = float(getattr(pos, "qty", 0) or 0)
            if qty <= 0:
                continue
            entry_price = float(getattr(pos, "avg_entry_price", 0) or 0)
            last_price = float(getattr(pos, "current_price", 0) or 0)
        except Exception:
            continue
        if entry_price <= 0 or last_price <= 0:
            continue

        if _should_skip_for_pending(symbol, orders):
            continue

        stop_order, trailing_order = _find_protection_orders(symbol, orders)
        stop_price = _extract_stop(stop_order) if stop_order else None

        if stop_order is None and trailing_order is None:
            if trailing_enable and trailing_mult > 0:
                trail_price = 0.0
                trail_percent = 2.0
                log_event(
                    f"RISK_PROTECT symbol={symbol} action=missing_protection creating_stop=trailing",
                    event="RISK",
                )
                try:  # pragma: no cover - network
                    broker.api.submit_order(
                        symbol=symbol,
                        side="sell",
                        qty=qty,
                        type="trailing_stop",
                        time_in_force=exec_cfg.get("time_in_force", "day"),
                        trail_price=trail_price if trail_price > 0 else None,
                        trail_percent=trail_percent,
                    )
                except Exception:
                    pass
            else:
                bracket = compute_bracket_prices(
                    symbol=symbol,
                    entry_price=entry_price,
                    atr=None,
                    risk_cfg=risk_cfg,
                    exec_cfg=exec_cfg,
                )
                stop_price = bracket["stop_price"]
                if stop_price and stop_price > 0:
                    stop_payload = {"stop_price": stop_price}
                    stop_limit = stop_limit_price(stop_price, symbol=symbol)
                    if stop_limit and stop_limit < stop_price:
                        stop_payload["limit_price"] = stop_limit
                    log_event(
                        f"RISK_PROTECT symbol={symbol} action=missing_protection creating_stop={stop_price}",
                        event="RISK",
                    )
                    try:  # pragma: no cover - network
                        broker.api.submit_order(
                            symbol=symbol,
                            side="sell",
                            qty=qty,
                            type="stop_limit" if "limit_price" in stop_payload else "stop",
                            time_in_force=exec_cfg.get("time_in_force", "day"),
                            **stop_payload,
                        )
                    except Exception:
                        pass
            continue

        if trailing_order is None and stop_price is not None:
            new_stop = compute_break_even_stop(
                symbol=symbol,
                entry_price=entry_price,
                initial_stop=stop_price,
                last_price=last_price,
                break_even_R=break_even_r,
                buffer_pct=break_even_buffer,
            )
            if new_stop and new_stop > stop_price:
                log_event(
                    (
                        "RISK_PROTECT "
                        f"symbol={symbol} action=move_stop_to_breakeven "
                        f"old_stop={stop_price:.4f} new_stop={new_stop:.4f} "
                        f"last={last_price:.4f} entry={entry_price:.4f} R={entry_price - stop_price:.4f}"
                    ),
                    event="RISK",
                )
                try:  # pragma: no cover - network
                    stop_id = getattr(stop_order, "id", None)
                    if stop_id:
                        broker.api.cancel_order(stop_id)
                    stop_payload = {"stop_price": new_stop}
                    stop_limit = stop_limit_price(new_stop, symbol=symbol)
                    if stop_limit and stop_limit < new_stop:
                        stop_payload["limit_price"] = stop_limit
                    broker.api.submit_order(
                        symbol=symbol,
                        side="sell",
                        qty=qty,
                        type="stop_limit" if "limit_price" in stop_payload else "stop",
                        time_in_force=exec_cfg.get("time_in_force", "day"),
                        **stop_payload,
                    )
                except Exception:
                    pass

        if trailing_enable and trailing_mult > 0 and trailing_order is None and stop_price:
            threshold = entry_price + 1.5 * (entry_price - stop_price)
            if last_price >= threshold:
                trail_price = 0.0
                trail_percent = 2.0
                log_event(
                    (
                        "RISK_PROTECT "
                        f"symbol={symbol} action=activate_trailing "
                        f"trail={trail_price if trail_price > 0 else trail_percent} "
                        f"last={last_price:.4f} entry={entry_price:.4f} atr=0"
                    ),
                    event="RISK",
                )
                try:  # pragma: no cover - network
                    if stop_order is not None:
                        stop_id = getattr(stop_order, "id", None)
                        if stop_id:
                            broker.api.cancel_order(stop_id)
                    broker.api.submit_order(
                        symbol=symbol,
                        side="sell",
                        qty=qty,
                        type="trailing_stop",
                        time_in_force=exec_cfg.get("time_in_force", "day"),
                        trail_price=trail_price if trail_price > 0 else None,
                        trail_percent=trail_percent,
                    )
                except Exception:
                    pass
