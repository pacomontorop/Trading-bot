from core.scheduler import start_schedulers, is_market_open
import time
from broker.alpaca import api, get_current_price
from utils.logger import log_event
from utils.emailer import send_email

def add_missing_trailing_stops(trail_percent=2.0):
    print("\U0001F527 Buscando posiciones abiertas sin trailing stop...")
    resumen = []
    try:
        open_orders = api.list_orders(status='open')
        symbols_with_any_order = {o.symbol for o in open_orders}

        positions = api.list_positions()
        for p in positions:
            symbol = p.symbol
            side = 'sell' if float(p.qty) > 0 else 'buy'
            qty_total = abs(int(float(p.qty)))
            qty_available = abs(int(float(p.qty_available)))
            current_price = float(p.current_price)
            trail_price = round(current_price * (trail_percent / 100), 2)

            if symbol in symbols_with_any_order:
                print(f"✅ {symbol} ya tiene alguna orden abierta (trailing, stop o take profit).")
                continue

            if qty_available <= 0:
                print(f"⚠️ {symbol} sin unidades disponibles para cubrir con trailing stop.")
                continue

            print(f"➕ Añadiendo trailing stop a {symbol}: {side} {qty_available} unidades")

            api.submit_order(
                symbol=symbol,
                qty=qty_available,
                side=side,
                type='trailing_stop',
                time_in_force='gtc',
                trail_price=trail_price
            )

            log_event(f"\U0001F527 Trailing stop añadido manualmente para {symbol}: {qty_available} unidades ({side})")
            resumen.append(f"{symbol}: {qty_available} unidades ({side}) a precio actual {current_price:.2f} con trail de {trail_price:.2f} USD")

        if resumen:
            subject = "\ud83d\udccc Trailing stops añadidos automáticamente"
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
    print("\U0001F7E2 Iniciando sistema de trading...", flush=True)

    while not is_market_open():
        print("⏳ Mercado cerrado. Esperando apertura para añadir trailing stops...", flush=True)
        time.sleep(60)

    add_missing_trailing_stops()  # ✅ Solo una vez al arrancar tras apertura del mercado

    start_schedulers()  # 🟢 Lanza los hilos

    while True:
        time.sleep(3600)  # Mantener vivo el proceso
