#monitor.py

import time
from broker.alpaca import api
from utils.logger import log_event
from core.executor import (
    open_positions,
    open_positions_lock,
    get_adaptive_trail_price,
)

def monitor_open_positions():
    print("🟢 Monitor de posiciones iniciado.")
    while True:
        try:
            positions = api.list_positions()
            symbols = {p.symbol for p in positions} if positions else set()
            with open_positions_lock:
                open_positions.intersection_update(symbols)
                open_positions.update(symbols)

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
                positions_data.append((symbol, qty, avg_entry_price, current_price, change_percent))

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

                trail_price = get_adaptive_trail_price(symbol)
                api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    type="trailing_stop",
                    time_in_force="gtc",
                    trail_price=trail_price,
                )
                log_event(f"🚨 Trailing stop de emergencia colocado para {symbol}")

        except Exception as e:
            log_event(f"❌ Error en watchdog_trailing_stop: {e}")

        time.sleep(600)
