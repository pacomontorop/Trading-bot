#monitor.py

import os
import time
from broker.alpaca import api, get_current_price
from utils.logger import log_event
from utils.orders import resolve_time_in_force
from core.executor import (
    open_positions,
    open_positions_lock,
    get_adaptive_trail_price,
    state_manager,
    entry_data,
    update_trailing_stop,
)
from utils.monitoring import update_positions_metric


TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "5"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-3"))
MAX_LOSS_USD = float(os.getenv("MAX_LOSS_USD", "50"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
TRAILING_WATCHDOG_INTERVAL = int(os.getenv("TRAILING_WATCHDOG_INTERVAL", "120"))


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
            return

        qty = abs(float(qty))
        qty_available = abs(float(qty_available))
        if qty <= 0 or qty_available <= 0:
            return

        if position_side.lower() == "long":
            gain_pct = (current_price - entry_price) / entry_price * 100
            unrealized = (current_price - entry_price) * qty
            close_side = "sell"
        else:
            gain_pct = (entry_price - current_price) / entry_price * 100
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
            symbols = {p.symbol for p in positions} if positions else set()
            with open_positions_lock:
                open_positions.intersection_update(symbols)
                open_positions.update(symbols)
                state_manager.replace_open_positions(open_positions)
            update_positions_metric(len(open_positions))

            if not positions:
                print("⚠️ No hay posiciones abiertas actualmente.")
                time.sleep(MONITOR_INTERVAL)
                continue

            positions_data = []
            for p in positions:
                symbol = p.symbol
                qty = float(p.qty)
                qty_available = float(getattr(p, "qty_available", p.qty))
                avg_entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)
                change_percent = (current_price - avg_entry_price) / avg_entry_price * 100

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

            for symbol, pos in pos_map.items():
                side = "sell" if pos.side.lower() == "long" else "buy"
                order = trailing_orders.get((symbol, side))

                qty = float(pos.qty)
                qty_available = float(getattr(pos, "qty_available", pos.qty))
                if qty <= 0 or qty_available <= 0:
                    continue

                if order is None:
                    reserved_qty = sum(
                        float(o.qty)
                        for o in open_orders
                        if o.symbol == symbol and o.side == side
                    )
                    available_qty = min(qty_available, qty - reserved_qty)
                    if available_qty <= 0:
                        log_event(
                            f"⚠️ Cantidad no disponible para {symbol}, reservada: {reserved_qty}"
                        )
                        continue

                    trail_price = get_adaptive_trail_price(symbol)
                    api.submit_order(
                        symbol=symbol,
                        qty=available_qty,
                        side=side,
                        type="trailing_stop",
                        time_in_force=resolve_time_in_force(
                            available_qty, asset_class=getattr(pos, "asset_class", "us_equity")
                        ),
                        trail_price=trail_price,
                    )
                    log_event(f"🚨 Trailing stop de emergencia colocado para {symbol}")
                    continue

                # Actualizar trailing stop existente con valor dinámico
                new_trail = get_adaptive_trail_price(symbol)
                current_trail = float(getattr(order, "trail_price", new_trail))
                if abs(new_trail - current_trail) > 0.01:
                    update_trailing_stop(symbol, order_id=order.id, trail_price=new_trail)

                # Mover a break-even si corresponde
                entry_price, _, _ = entry_data.get(symbol, (None, None, None))
                stop_price = float(getattr(order, "stop_price", 0))
                current_price = get_current_price(symbol)
                if (
                    entry_price
                    and current_price
                    and current_price > entry_price
                    and stop_price < entry_price
                ):
                    hwm = float(getattr(order, "hwm", current_price))
                    be_trail = max(hwm - entry_price, 0.01)
                    update_trailing_stop(symbol, order_id=order.id, trail_price=be_trail)

        except Exception as e:
            log_event(f"❌ Error en watchdog_trailing_stop: {e}")

        time.sleep(TRAILING_WATCHDOG_INTERVAL)
