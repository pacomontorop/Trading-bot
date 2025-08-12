#monitor.py

import time
from broker.alpaca import api, get_current_price
from utils.logger import log_event
from core.executor import (
    open_positions,
    open_positions_lock,
    get_adaptive_trail_price,
    state_manager,
)
from utils.monitoring import update_positions_metric


def check_virtual_take_profit_and_stop(symbol, entry_price, qty, position_side):
    """Cierra la posición si alcanza un take profit virtual (+7%) o stop loss virtual (-5%)."""
    try:
        current_price = get_current_price(symbol)
        if current_price is None or entry_price is None or qty is None:
            return

        qty = int(abs(float(qty)))
        if qty <= 0:
            return

        if position_side.lower() == "long":
            gain_pct = (current_price - entry_price) / entry_price * 100
            close_side = "sell"
        else:
            gain_pct = (entry_price - current_price) / entry_price * 100
            close_side = "buy"

        if gain_pct >= 7 or gain_pct <= -5:
            open_orders = api.list_orders(status="open")
            reserved_qty = sum(
                int(float(o.qty))
                for o in open_orders
                if o.symbol == symbol and o.side == close_side
            )
            available_qty = qty - reserved_qty
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
                time_in_force="gtc",
            )

            if gain_pct >= 7:
                log_event(
                    f"📈 Take profit virtual ejecutado en {symbol} con +{gain_pct:.2f}%"
                )
            else:
                log_event(
                    f"📉 Stop loss virtual ejecutado en {symbol} con {gain_pct:.2f}%"
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
                time.sleep(900)
                continue

            positions_data = []
            for p in positions:
                symbol = p.symbol
                qty = float(p.qty)
                avg_entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)
                change_percent = (current_price - avg_entry_price) / avg_entry_price * 100

                if symbol in open_positions:
                    check_virtual_take_profit_and_stop(
                        symbol, avg_entry_price, qty, getattr(p, "side", "long")
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

        time.sleep(900)


def watchdog_trailing_stop():
    """Reinstala trailing stops perdidos cada 10 minutos."""
    print("🟢 Watchdog trailing stop iniciado.")
    while True:
        try:
            positions = api.list_positions()
            pos_map = {p.symbol: p for p in positions} if positions else {}

            open_orders = api.list_orders(status="open")
            trailing = {
                (o.symbol, o.side)
                for o in open_orders
                if getattr(o, "type", "") == "trailing_stop"
            }

            for symbol, pos in pos_map.items():
                side = "sell" if pos.side.lower() == "long" else "buy"
                if (symbol, side) in trailing:
                    continue

                qty = int(float(pos.qty))
                if qty <= 0:
                    continue

                reserved_qty = sum(
                    int(float(o.qty))
                    for o in open_orders
                    if o.symbol == symbol and o.side == side
                )
                available_qty = qty - reserved_qty
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
                    time_in_force="gtc",
                    trail_price=trail_price,
                )
                log_event(f"🚨 Trailing stop de emergencia colocado para {symbol}")

        except Exception as e:
            log_event(f"❌ Error en watchdog_trailing_stop: {e}")

        time.sleep(600)
