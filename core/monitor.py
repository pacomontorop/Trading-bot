#monitor.py

import json
import math
import os
import time
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from broker.alpaca import api, is_market_open
from core.broker import get_tick_size, round_to_tick
from core.executor import (
    _tick_rounding_enabled,
    compute_chandelier_trail,
    entry_data,
    get_adaptive_trail_price,
    open_positions,
    open_positions_lock,
    state_manager,
    update_stop_order,
    update_trailing_stop,
)
from data.providers import (
    PRICE_FRESHNESS_SEC_CRYPTO,
    PRICE_FRESHNESS_SEC_EQ,
    get_price,
)
from libs.broker.ticks import ceil_to_tick, equity_tick_for, floor_to_tick, round_stop_price
import config
from utils.health import snapshot as health_snapshot
from utils.logger import log_event
from utils.monitoring import update_positions_metric
from utils.orders import resolve_time_in_force
from utils.symbols import detect_asset_class


EPS = 1e-9


def get_current_price(symbol: str, kind: str | None = None) -> float | None:
    """Compatibility wrapper returning a float price for ``symbol``."""

    price, *_ = get_price(symbol, kind)
    return float(price) if price is not None else None


def _fmt_missing(missing: dict[str, str]) -> str:
    if not missing:
        return "{}"
    return json.dumps(missing, separators=(",", ":"))


def validate_position_inputs(
    symbol: str,
    price,
    price_ts: datetime | None,
    entry,
    qty,
    meta: dict | None,
    max_age_s: int | None,
) -> tuple[bool, dict[str, str], float | None, float | None, float | None]:
    missing: dict[str, str] = {}

    price_val: float | None = None
    if price is None:
        missing["price"] = "None"
    else:
        try:
            price_val = float(price)
        except Exception:
            missing["price"] = "invalid"
        else:
            if not math.isfinite(price_val) or price_val <= 0:
                missing["price"] = f"{price_val}"

    entry_val: float | None = None
    if entry is None:
        missing["entry"] = "None"
    else:
        try:
            entry_val = float(entry)
        except Exception:
            missing["entry"] = "invalid"
        else:
            if not math.isfinite(entry_val) or entry_val <= 0:
                missing["entry"] = f"{entry_val}"

    qty_val: float | None = None
    if qty is None:
        missing["qty"] = "None"
    else:
        try:
            qty_val = float(qty)
        except Exception:
            missing["qty"] = "invalid"
        else:
            if qty_val <= 0:
                missing["qty"] = f"{qty_val}"

    tick = (meta or {}).get("tick_price") if meta else None
    if tick is None:
        missing["tick_size"] = "None"

    stale_reason = (meta or {}).get("stale_reason") if meta else None
    stale_allowed = bool((meta or {}).get("stale_allowed")) if meta else False
    if stale_reason and not stale_allowed:
        missing["freshness"] = stale_reason
    elif price_ts is not None and max_age_s:
        try:
            age = (datetime.now(timezone.utc) - price_ts).total_seconds()
            if age > max_age_s:
                missing["freshness"] = f"stale>{max_age_s}"
        except Exception:
            missing.setdefault("freshness", "age_error")
    elif price_ts is None and max_age_s:
        missing.setdefault("freshness", "ts_missing")

    ok = len(missing) == 0
    return ok, missing, price_val, entry_val, qty_val


TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "5"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-3"))
MAX_LOSS_USD = float(os.getenv("MAX_LOSS_USD", "50"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
TRAILING_WATCHDOG_INTERVAL = int(os.getenv("TRAILING_WATCHDOG_INTERVAL", "120"))
CANCEL_ORDERS_INTERVAL = int(os.getenv("CANCEL_ORDERS_INTERVAL", "300"))
STALE_ORDER_MINUTES = int(os.getenv("STALE_ORDER_MINUTES", "15"))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def check_virtual_take_profit_and_stop(
    symbol, entry_price, qty, qty_available, position_side, asset_class
):
    """Cierra la posición si alcanza un take profit virtual (+5%), stop loss (-3%) o pérdida monetaria (-50 USD).

    Usa ``qty_available`` para evitar intentar cerrar más cantidad de la disponible cuando
    ya existen órdenes abiertas (por ejemplo un trailing stop)."""
    try:
        asset_kind = "crypto" if (asset_class or "").lower() == "crypto" else "equity"
        price_dec, price_ts, provider, stale, stale_reason = get_price(symbol, asset_kind)
        tick = get_tick_size(symbol, asset_class, float(price_dec) if price_dec else None)
        meta = {
            "tick_price": tick,
            "provider": provider,
            "stale": stale,
            "stale_reason": stale_reason,
            "stale_allowed": bool(stale),
        }
        freshness = (
            PRICE_FRESHNESS_SEC_CRYPTO if asset_kind == "crypto" else PRICE_FRESHNESS_SEC_EQ
        )
        ok, missing, current_price, entry_val, qty_val = validate_position_inputs(
            symbol,
            price_dec,
            price_ts,
            entry_price,
            qty,
            meta,
            freshness,
        )

        try:
            qty_available_val = float(qty_available) if qty_available is not None else None
        except Exception:
            qty_available_val = None
        if qty_available_val is None or qty_available_val <= 0:
            missing["qty_available"] = str(qty_available)
            ok = False

        if not ok:
            log_event(
                f"MONITOR {symbol}: skip (incomplete) missing={_fmt_missing(missing)}",
                event="REPORT",
            )
            return

        qty = abs(qty_val or 0.0)
        qty_available = abs(qty_available_val)
        entry_price = entry_val or 0.0
        if qty <= 0 or qty_available <= 0:
            log_event(
                f"MONITOR {symbol}: skip (qty<=0)",
                event="REPORT",
            )
            return

        if position_side.lower() == "long":
            gain_pct = (current_price - entry_price) / max(entry_price, EPS) * 100
            unrealized = (current_price - entry_price) * qty
            close_side = "sell"
        else:
            gain_pct = (entry_price - current_price) / max(entry_price, EPS) * 100
            unrealized = (entry_price - current_price) * qty
            close_side = "buy"

        if (
            gain_pct >= TAKE_PROFIT_PCT
            or gain_pct <= STOP_LOSS_PCT
            or unrealized <= -MAX_LOSS_USD
        ):
            open_orders = api.list_orders(status="open")
            reserved_qty = sum(
                float(o.qty)
                for o in open_orders
                if o.symbol == symbol and o.side == close_side
            )
            available_qty = min(qty_available, qty - reserved_qty)
            if available_qty <= 0:
                log_event(
                    f"⚠️ Cantidad no disponible para {symbol}, reservada: {reserved_qty}"
                )
                return

            api.submit_order(
                symbol=symbol,
                qty=available_qty,
                side=close_side,
                type="market",
                time_in_force=resolve_time_in_force(
                    available_qty, asset_class=asset_class
                ),
            )

            if gain_pct >= TAKE_PROFIT_PCT:
                log_event(
                    f"📈 Take profit virtual ejecutado en {symbol} con +{gain_pct:.2f}%"
                )
            elif gain_pct <= STOP_LOSS_PCT:
                log_event(
                    f"📉 Stop loss virtual ejecutado en {symbol} con {gain_pct:.2f}%"
                )
            else:
                log_event(
                    f"📉 Stop monetario ejecutado en {symbol} con {unrealized:.2f} USD"
                )
            return

    except Exception as e:
        log_event(f"⚠️ Error en check_virtual_take_profit_and_stop para {symbol}: {e}")

def monitor_open_positions():
    print("🟢 Monitor de posiciones iniciado.")
    while True:
        try:
            positions = api.list_positions()
            pos_map = {
                p.symbol: {
                    "coid": getattr(p, "client_order_id", ""),
                    "qty": float(getattr(p, "qty", 0)),
                    "avg": float(getattr(p, "avg_entry_price", 0)),
                }
                for p in positions
            } if positions else {}
            symbols = set(pos_map.keys())
            with open_positions_lock:
                open_positions.intersection_update(symbols)
                open_positions.update(symbols)
                state_manager.replace_open_positions(pos_map)
            update_positions_metric(len(open_positions))

            if not positions:
                print("⚠️ No hay posiciones abiertas actualmente.")
                time.sleep(MONITOR_INTERVAL)
                continue

            positions_data = []
            for p in positions:
                symbol = p.symbol
                raw_qty = getattr(p, "qty", None)
                if raw_qty is None:
                    log_event(
                        f"MONITOR {symbol}: skip (qty is None)",
                        event="REPORT",
                    )
                    continue

                qty = _safe_float(raw_qty, 0.0)
                qty_available = _safe_float(
                    getattr(p, "qty_available", getattr(p, "qty", 0.0)), 0.0
                )
                avg_entry_price = _safe_float(getattr(p, "avg_entry_price", None), 0.0)
                asset_class = getattr(p, "asset_class", "us_equity") or "us_equity"
                asset_kind = "crypto" if asset_class.lower() == "crypto" else "equity"
                price_dec, price_ts, provider, stale, stale_reason = get_price(
                    symbol, asset_kind
                )
                tick = get_tick_size(symbol, asset_class, float(price_dec) if price_dec else None)
                meta = {
                    "tick_price": tick,
                    "provider": provider,
                    "stale": stale,
                    "stale_reason": stale_reason,
                    "stale_allowed": bool(stale),
                }
                freshness = (
                    PRICE_FRESHNESS_SEC_CRYPTO
                    if asset_kind == "crypto"
                    else PRICE_FRESHNESS_SEC_EQ
                )
                ok, missing, current_price, entry_val, qty_val = validate_position_inputs(
                    symbol,
                    price_dec,
                    price_ts,
                    avg_entry_price,
                    qty,
                    meta,
                    freshness,
                )

                if qty_available <= 0:
                    missing["qty_available"] = str(qty_available)
                    ok = False

                if not ok:
                    log_event(
                        f"MONITOR {symbol}: skip (incomplete) missing={_fmt_missing(missing)}",
                        event="REPORT",
                    )
                    continue

                qty = abs(qty_val or 0.0)
                qty_available = abs(qty_available)
                avg_entry_price = entry_val or 0.0
                if qty <= 0 or qty_available <= 0:
                    log_event(
                        f"MONITOR {symbol}: skip (qty<=0)",
                        event="REPORT",
                    )
                    continue
                if current_price is None or current_price <= 0:
                    log_event(
                        f"MONITOR {symbol}: skip (price invalid)",
                        event="REPORT",
                    )
                    continue
                change_percent = (
                    (current_price - avg_entry_price) / max(avg_entry_price, EPS) * 100
                )

                entry_ts = entry_data.get(symbol, (None, None, None))[2]
                if change_percent <= -10 or (
                    entry_ts and datetime.utcnow() - entry_ts > timedelta(days=30)
                ):
                    log_event(
                        f"🔍 Revisión recomendada para {symbol}: {change_percent:.2f}% desde entrada"
                    )

                if symbol in open_positions:
                    check_virtual_take_profit_and_stop(
                        symbol,
                        avg_entry_price,
                        qty,
                        qty_available,
                        getattr(p, "side", "long"),
                        getattr(p, "asset_class", "us_equity"),
                    )

                positions_data.append(
                    (symbol, qty, avg_entry_price, current_price, change_percent)
                )

            top_positions = sorted(positions_data, key=lambda x: abs(x[4]), reverse=True)[:5]

            print("📈 Top 5 cambios relativos de posiciones abiertas:")
            for symbol, qty, avg_entry_price, current_price, change_percent in top_positions:
                print(f"🔹 {symbol}: {qty} unidades")
                print(f"   Entrada: {avg_entry_price} | Actual: {current_price}")
                print(f"   Cambio: {change_percent:.2f}%")
                print("-" * 40)

            log_event("✅ Monitorización de posiciones completada correctamente.")

            stats = health_snapshot()
            prices_stats = stats.get("prices", {})
            scan_stats = stats.get("scans", {})
            log_event(
                (
                    "HEALTH "
                    f"prices_ok={prices_stats.get('ok', 0)} "
                    f"prices_stale={prices_stats.get('stale', 0)} "
                    f"prices_failed={prices_stats.get('failed', 0)} "
                    f"equities_scanned={scan_stats.get('equity', 0)} "
                    f"crypto_scanned={scan_stats.get('crypto', 0)}"
                ),
                event="REPORT",
            )

        except Exception as e:
            print(f"❌ Error monitorizando posiciones: {e}")
            log_event(f"❌ Error monitorizando posiciones: {e}")

        time.sleep(MONITOR_INTERVAL)


def watchdog_trailing_stop():
    """Reinstala trailing stops perdidos periódicamente."""
    print("🟢 Watchdog trailing stop iniciado.")
    while True:
        try:
            positions = api.list_positions()
            pos_map = {p.symbol: p for p in positions} if positions else {}

            open_orders = api.list_orders(status="open")
            trailing_orders = {
                (o.symbol, o.side): o
                for o in open_orders
                if getattr(o, "type", "") == "trailing_stop"
            }
            stop_orders = {
                (o.symbol, o.side): o
                for o in open_orders
                if getattr(o, "type", "") in ("stop", "stop_limit")
            }

            for symbol, pos in pos_map.items():
                if detect_asset_class(symbol) != "equity":
                    continue

                side = "sell" if pos.side.lower() == "long" else "buy"
                qty = _safe_float(getattr(pos, "qty", 0.0), 0.0)
                qty_available = _safe_float(
                    getattr(pos, "qty_available", getattr(pos, "qty", 0.0)), 0.0
                )
                entry_price_val = _safe_float(getattr(pos, "avg_entry_price", None), 0.0)
                asset_class = getattr(pos, "asset_class", "us_equity") or "us_equity"
                if qty <= 0 or qty_available <= 0:
                    log_event(
                        f"MONITOR {symbol}: skip (qty<=0)", event="REPORT"
                    )
                    continue
                if entry_price_val <= 0:
                    log_event(
                        f"MONITOR {symbol}: skip (entry_price invalid)",
                        event="REPORT",
                    )
                    continue
                is_fractional = abs(qty - round(qty)) > 1e-6
                order = (
                    stop_orders.get((symbol, side))
                    if is_fractional
                    else trailing_orders.get((symbol, side))
                )

                if order is None:
                    reserved_qty = sum(
                        _safe_float(o.qty, 0.0)
                        for o in open_orders
                        if o.symbol == symbol and o.side == side
                    )
                    available_qty = min(qty_available, qty - reserved_qty)
                    if available_qty <= 0:
                        log_event(
                            f"⚠️ Cantidad no disponible para {symbol}, reservada: {reserved_qty}"
                        )
                        continue

                    entry_price = entry_price_val
                    asset_kind = "crypto" if asset_class.lower() == "crypto" else "equity"
                    price_dec, _, _, _, _ = get_price(symbol, asset_kind)
                    current_price = _safe_float(price_dec, 0.0)
                    if entry_price <= 0 or current_price <= 0:
                        log_event(
                            f"MONITOR {symbol}: skip (price invalid)",
                            event="REPORT",
                        )
                        continue

                    risk_cfg = (config._policy or {}).get("risk", {})
                    atr_k = float(risk_cfg.get("atr_k", 2.0)) or 1.0
                    _, _, _, stop_hint = entry_data.get(symbol, (None, None, None, None))
                    stop_hint = _safe_float(stop_hint, 0.0)
                    atr_hint = stop_hint / atr_k if atr_k > 0 else stop_hint
                    trail_dist = compute_chandelier_trail(
                        current_price, atr_hint, config._policy
                    )
                    if trail_dist is None or trail_dist <= 0:
                        min_tr = float(risk_cfg.get("min_trailing_pct", 0.005))
                        trail_dist = max(min_tr * current_price, 0.01 * current_price)

                    asset_class = getattr(pos, "asset_class", "us_equity") or "us_equity"
                    tick = (
                        get_tick_size(symbol, asset_class, current_price)
                        if _tick_rounding_enabled(config._policy)
                        else None
                    )
                    tick_dec = Decimal(str(tick)) if tick else None

                    if is_fractional:
                        current_dec = Decimal(str(current_price))
                        trail_dec = Decimal(str(trail_dist))
                        raw_stop = (
                            current_dec - trail_dec
                            if side == "sell"
                            else current_dec + trail_dec
                        )
                        stop_dec = round_stop_price(
                            symbol,
                            side,
                            raw_stop,
                            asset_class=asset_class,
                            tick_override=tick_dec,
                        )
                        last_price_dec = current_dec
                        if (
                            side == "sell"
                            and stop_dec is not None
                            and stop_dec >= last_price_dec
                        ):
                            step = tick_dec or equity_tick_for(last_price_dec)
                            adjust_basis = last_price_dec - step
                            if adjust_basis > 0:
                                stop_dec = round_stop_price(
                                    symbol,
                                    side,
                                    adjust_basis,
                                    asset_class=asset_class,
                                    tick_override=tick_dec or step,
                                )
                        elif (
                            side == "buy"
                            and stop_dec is not None
                            and stop_dec <= last_price_dec
                        ):
                            step = tick_dec or equity_tick_for(last_price_dec)
                            stop_dec = round_stop_price(
                                symbol,
                                side,
                                last_price_dec + step,
                                asset_class=asset_class,
                                tick_override=tick_dec or step,
                            )
                        stop_price_value = float(stop_dec) if stop_dec is not None else None
                        try:
                            api.submit_order(
                                symbol=symbol,
                                qty=available_qty,
                                side=side,
                                type="stop",
                                time_in_force=resolve_time_in_force(
                                    available_qty,
                                    asset_class=asset_class,
                                ),
                                stop_price=stop_price_value,
                            )
                        except Exception as e:
                            if "sub-penny" in str(e).lower() and stop_dec is not None and tick_dec is not None:
                                adjust_dec = (
                                    ceil_to_tick(stop_dec, tick_dec)
                                    if side == "sell"
                                    else floor_to_tick(stop_dec, tick_dec)
                                )
                                api.submit_order(
                                    symbol=symbol,
                                    qty=available_qty,
                                    side=side,
                                    type="stop",
                                    time_in_force=resolve_time_in_force(
                                        available_qty,
                                        asset_class=asset_class,
                                    ),
                                    stop_price=float(adjust_dec),
                                )
                            else:
                                raise
                        log_event(
                            f"🚨 Stop dinámico inicial colocado para {symbol}"
                        )
                    else:
                        if tick:
                            trail_dist = round_to_tick(trail_dist, tick)
                        api.submit_order(
                            symbol=symbol,
                            qty=available_qty,
                            side=side,
                            type="trailing_stop",
                            time_in_force=resolve_time_in_force(
                                available_qty,
                                asset_class=asset_class,
                            ),
                            trail_price=trail_dist,
                        )
                        log_event(
                            f"🚨 Trailing stop de emergencia colocado para {symbol}"
                        )
                    continue

                asset_kind = "crypto" if asset_class.lower() == "crypto" else "equity"
                price_dec, _, _, _, _ = get_price(symbol, asset_kind)
                current_price = _safe_float(price_dec, 0.0)
                if current_price <= 0:
                    continue

                trail = _safe_float(get_adaptive_trail_price(symbol), 0.0)
                if trail <= 0:
                    risk_cfg = (config._policy or {}).get("risk", {})
                    min_tr = float(risk_cfg.get("min_trailing_pct", 0.005))
                    trail = max(min_tr * current_price, 0.01 * current_price)
                asset_class = getattr(pos, "asset_class", "us_equity") or "us_equity"
                tick = (
                    get_tick_size(symbol, asset_class, current_price)
                    if _tick_rounding_enabled(config._policy)
                    else None
                )
                if tick:
                    trail = round_to_tick(trail, tick)

                if is_fractional:
                    current_dec = Decimal(str(current_price))
                    trail_dec = Decimal(str(trail))
                    raw_stop = (
                        current_dec - trail_dec
                        if side == "sell"
                        else current_dec + trail_dec
                    )
                    tick_dec = Decimal(str(tick)) if tick else None
                    new_stop_dec = round_stop_price(
                        symbol,
                        side,
                        raw_stop,
                        asset_class=asset_class,
                        tick_override=tick_dec,
                    )
                    if new_stop_dec is not None:
                        if (
                            side == "sell"
                            and new_stop_dec >= current_dec
                        ):
                            step = tick_dec or equity_tick_for(current_dec)
                            adjust_basis = current_dec - step
                            if adjust_basis > 0:
                                new_stop_dec = round_stop_price(
                                    symbol,
                                    side,
                                    adjust_basis,
                                    asset_class=asset_class,
                                    tick_override=tick_dec or step,
                                )
                        elif (
                            side == "buy"
                            and new_stop_dec <= current_dec
                        ):
                            step = tick_dec or equity_tick_for(current_dec)
                            new_stop_dec = round_stop_price(
                                symbol,
                                side,
                                current_dec + step,
                                asset_class=asset_class,
                                tick_override=tick_dec or step,
                            )
                    new_stop = float(new_stop_dec) if new_stop_dec is not None else None
                    if new_stop is None:
                        continue
                    current_stop = _safe_float(
                        getattr(order, "stop_price", new_stop), new_stop
                    )
                    if (
                        (side == "sell" and new_stop > current_stop + 0.01)
                        or (side == "buy" and new_stop < current_stop - 0.01)
                    ):
                        update_stop_order(
                            symbol,
                            order_id=order.id,
                            stop_price=new_stop,
                            side=side,
                        )
                    entry_price, _, _ = entry_data.get(symbol, (None, None, None))
                    entry_price = _safe_float(entry_price, 0.0)
                    if entry_price > 0:
                        if (
                            side == "sell"
                            and current_price > entry_price
                            and current_stop < entry_price
                        ):
                            update_stop_order(
                                symbol,
                                order_id=order.id,
                                stop_price=entry_price,
                                side=side,
                            )
                        elif (
                            side == "buy"
                            and current_price < entry_price
                            and current_stop > entry_price
                        ):
                            update_stop_order(
                                symbol,
                                order_id=order.id,
                                stop_price=entry_price,
                                side=side,
                            )
                else:
                    new_trail = trail
                    current_trail = _safe_float(
                        getattr(order, "trail_price", new_trail), new_trail
                    )
                    if abs(new_trail - current_trail) > 0.01:
                        update_trailing_stop(
                            symbol,
                            order_id=order.id,
                            trail_price=new_trail,
                            side=side,
                        )
                    entry_price, _, _ = entry_data.get(symbol, (None, None, None))
                    stop_price = float(getattr(order, "stop_price", 0))
                    if (
                        entry_price
                        and current_price > entry_price
                        and stop_price < entry_price
                    ):
                        hwm = float(getattr(order, "hwm", current_price))
                        be_trail = max(hwm - entry_price, 0.01)
                        update_trailing_stop(
                            symbol, order_id=order.id, trail_price=be_trail
                        )

        except Exception as e:
            log_event(f"❌ Error en watchdog_trailing_stop: {e}")

        time.sleep(TRAILING_WATCHDOG_INTERVAL)


def cancel_stale_orders_loop():
    """Cancel pending orders that are no longer relevant."""
    while True:
        try:
            now = datetime.utcnow()
            open_orders = api.list_orders(status="open")
            for o in open_orders:
                submitted = getattr(o, "submitted_at", None)
                tif = getattr(o, "time_in_force", "")
                otype = getattr(o, "type", "")
                if not submitted or otype in ("trailing_stop", "stop", "stop_limit"):
                    continue

                age_min = (now - submitted.replace(tzinfo=None)).total_seconds() / 60
                if age_min > STALE_ORDER_MINUTES or (
                    tif == "day" and not is_market_open()
                ):
                    try:
                        api.cancel_order(o.id)
                        log_event(f"🗑️ Orden cancelada por antigüedad: {o.symbol}")
                    except Exception as e:
                        log_event(f"⚠️ Error cancelando orden {o.id}: {e}")
        except Exception as e:
            log_event(f"⚠️ Error en cancel_stale_orders_loop: {e}")
        time.sleep(CANCEL_ORDERS_INTERVAL)
