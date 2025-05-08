from core.scheduler import start_schedulers, is_market_open
import time
from broker.alpaca import api, get_current_price
from utils.logger import log_event
from utils.emailer import send_email

def add_missing_trailing_stops(trail_percent=2.0):
    print("🔧 Buscando posiciones abiertas sin trailing stop...")
    resumen = []
    try:
        open_orders = api.list_orders(status='open')
        symbols_with_any_order = {o.symbol for o in open_orders if o.order_type in ['trailing_stop', 'stop', 'limit']}

        positions = api.list_positions()
        for p in positions:
            symbol = p.symbol
            if symbol in symbols_with_any_order:
                print(f"✅ {symbol} ya tiene una orden activa (trailing/stop/limit).")
                continue

            side = 'sell' if float(p.qty) > 0 else 'buy'
            qty = abs(int(float(p.qty)))
            current_price = float(p.current_price)
            trail_price = round(current_price * (trail_percent / 100), 2)

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
            resumen.append(f"{symbol}: {qty} unidades ({side}) a precio actual {current_price:.2f} con trail de {trail_price:.2f} USD")

        if resumen:
            subject = "📌 Trailing stops añadidos automáticamente"
            body = "Se han añadido los siguientes trailing stops manualmente tras detectar que estaban ausentes:\n\n"
            body += "\n".join(resumen)
            send_email(subject, body)
        else:
            print("✅ No se encontraron posiciones sin trailing stop.")
    except Exception as e:
        error_msg = f"❌ Error añadiendo trailing stops: {e}"
        print(error_msg)
        log_event(error_msg)
        send_email("❌ Error en trailing stops iniciales", error_msg)

if __name__ == "__main__":
    print("🟢 Iniciando sistema de trading...", flush=True)

    # Esperar a que abra el mercado
    while not is_market_open():
        print("⏳ Mercado cerrado. Esperando apertura para añadir trailing stops...", flush=True)
        time.sleep(60)

    # Ejecutar una única vez al abrir mercado
    add_missing_trailing_stops()

    # Lanzar schedulers
    start_schedulers()

    # Mantener vivo el proceso
    while True:
        time.sleep(3600)
