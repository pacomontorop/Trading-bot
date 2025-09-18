#monitor.py

import os
import time
import math
from broker.alpaca import api, get_current_price, is_market_open
from utils.logger import log_event
from utils.orders import resolve_time_in_force
from datetime import datetime, timedelta
from core.executor import (
    open_positions,
    open_positions_lock,
    get_adaptive_trail_price,
    state_manager,
    entry_data,
    update_trailing_stop,
    update_stop_order,
    compute_chandelier_trail,
    _tick_rounding_enabled,
)
from utils.monitoring import update_positions_metric
import config
from utils.symbols import detect_asset_class
from core.broker import get_tick_size, round_to_tick


EPS = 1e-9


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
        current_price = get_current_price(symbol)
        if (
            current_price is None
            or entry_price is None
            or qty is None
            or qty_available is None
        ):
            log_event(
                f"MONITOR {symbol}: skip (datos incompletos)",
                event="REPORT",
            )
            return

        qty = abs(float(qty))
        qty_available = abs(float(qty_available))
        if qty <= 0 or qty_available <= 0:
            log_event(
                f"MONITOR {symbol}: skip (qty<=0)",
                event="REPORT",
            )
            return

        if position_side.lower() == "long":
            if not entry_price or entry_price <= 0:
                log_event(
                    f"MONITOR {symbol}: skip (entry_price invalid)",
                    event="REPORT",
                )
                return
            gain_pct = (current_price - entry_price) / max(entry_price, EPS) * 100
            unrealized = (current_price - entry_price) * qty
            close_side = "sell"
        else:
            if not entry_price or entry_price <= 0:
                log_event(
                    f"MONITOR {symbol}: skip (entry_price invalid)",
                    event="REPORT",
                )
                return
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
                qty = float(raw_qty)
                qty_available = float(getattr(p, "qty_available", p.qty))
                avg_entry_price = float(getattr(p, "avg_entry_price", 0.0) or 0.0)
                current_price = float(getattr(p, "current_price", 0.0) or 0.0)
                if qty <= 0 or qty_available <= 0:
                    log_event(
                        f"MONITOR {symbol}: skip (qty<=0)",
                        event="REPORT",
                    )
                    continue
                if avg_entry_price <= 0 or current_price <= 0:
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
                    current_price = _safe_float(get_current_price(symbol), 0.0)
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

                    tick = get_tick_size(
                        symbol,
                        getattr(pos, "asset_class", "us_equity"),
                        current_price,
                    ) if _tick_rounding_enabled(config._policy) else None

                    if is_fractional:
                        stop_price = (
                            current_price - trail_dist
                            if side == "sell"
                            else current_price + trail_dist
                        )
                        if tick:
                            mode = "down" if side == "sell" else "up"
                            stop_price = round_to_tick(stop_price, tick, mode=mode)
                        api.submit_order(
                            symbol=symbol,
                            qty=available_qty,
                            side=side,
                            type="stop",
                            time_in_force=resolve_time_in_force(
                                available_qty,
                                asset_class=getattr(pos, "asset_class", "us_equity"),
                            ),
                            stop_price=stop_price,
                        )
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
                                asset_class=getattr(pos, "asset_class", "us_equity"),
                            ),
                            trail_price=trail_dist,
                        )
                        log_event(
                            f"🚨 Trailing stop de emergencia colocado para {symbol}"
                        )
                    continue

                current_price = _safe_float(get_current_price(symbol), 0.0)
                if current_price <= 0:
                    continue

                trail = _safe_float(get_adaptive_trail_price(symbol), 0.0)
                if trail <= 0:
                    risk_cfg = (config._policy or {}).get("risk", {})
                    min_tr = float(risk_cfg.get("min_trailing_pct", 0.005))
                    trail = max(min_tr * current_price, 0.01 * current_price)
                tick = get_tick_size(
                    symbol,
                    getattr(pos, "asset_class", "us_equity"),
                    current_price,
                ) if _tick_rounding_enabled(config._policy) else None
                if tick:
                    trail = round_to_tick(trail, tick)

                if is_fractional:
                    new_stop = (
                        current_price - trail if side == "sell" else current_price + trail
                    )
                    if tick:
                        mode = "down" if side == "sell" else "up"
                        new_stop = round_to_tick(new_stop, tick, mode=mode)
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
