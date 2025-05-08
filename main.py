from core.scheduler import start_schedulers
import time
from broker.alpaca import api, get_current_price
from utils.logger import log_event

def add_missing_trailing_stops(trail_percent=2.0):
    print("🔧 Buscando posiciones abiertas sin trailing stop...")
    try:
        open_orders = api.list_orders(status='open')
        open_order_symbols = {o.symbol for o in open_orders if o.order_type == 'trailing_stop'}

        positions = api.list_positions()
        for p in positions:
            symbol = p.symbol
            side = 'sell' if float(p.qty) > 0 else 'buy'
            qty = abs(int(float(p.qty)))
            current_price = float(p.current_price)
            trail_price = round(current_price * (trail_percent / 100), 2)

            if symbol in open_order_symbols:
                print(f"✅ {symbol} ya tiene trailing stop.")
                continue

            print(f"➕ Añadiendo trailing stop a {symbol}: {side} {qty} unidades")

            api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type='trailing_stop',
                time_in_force='gtc',
                trail_price=trail_price
            )

            log_event(f"🔧 Trailing stop añadido manualmente para {symbol}: {qty} unidades ({side})")
    except Exception as e:
        print(f"❌ Error añadiendo trailing stops: {e}")
        log_event(f"❌ Error añadiendo trailing stops: {e}")

if __name__ == "__main__":
    print("🟢 Lanzando schedulers...", flush=True)
    start_schedulers()

    # 🛠 Ejecuta esta corrección UNA VEZ al arrancar
    add_missing_trailing_stops()

    # 🔁 Mantener vivo el proceso aunque todos los hilos sean daemon
    while True:
        time.sleep(3600)
