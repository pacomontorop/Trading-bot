import threading
import time
from datetime import datetime

from core.executor import (
    place_order_with_trailing_stop,
    place_short_order_with_trailing_buy,
    pending_opportunities,
    pending_trades,
    invested_today_usd
)
from broker.alpaca import api, get_current_price, is_market_open
from signals.reader import get_top_signals, get_top_shorts
from signals.filters import is_market_volatile_or_low_volume
from utils.emailer import send_email
from utils.logger import log_event
from core.monitor import monitor_open_positions

def pre_market_scan():
    while True:
        now = datetime.utcnow()
        current_hour = now.hour

        if is_market_open():
            if is_market_volatile_or_low_volume():
                log_event("⚠️ Día demasiado volátil o con volumen bajo. No se operan acciones.")
                print("😴 No operamos en acciones hoy.")
                print("⚠️ Saltando compra por volumen o volatilidad.")
            else:
                print("🔍 Buscando oportunidades en acciones...")
                opportunities = get_top_signals(asset_type="stocks", min_criteria=5)
                for symbol in opportunities:
                    place_order_with_trailing_stop(symbol, 1000, 2.0)
                    pending_opportunities.add(symbol)
        else:
            print("⏳ Mercado cerrado para acciones.")

        log_event(f"🟢 Total invertido en este ciclo de compra long: {invested_today_usd:.2f} USD")

        if current_hour in range(13, 15):
            time.sleep(300)
        elif current_hour in range(19, 22):
            time.sleep(300)
        elif current_hour in range(15, 19):
            time.sleep(600)
        else:
            time.sleep(1800)

def crypto_scan():
    while True:
        if is_market_volatile_or_low_volume():
            log_event("⚠️ Día demasiado volátil o volumen bajo. No se operan criptos.")
            print("😴 No operamos en cripto hoy.")
        else:
            print("🔍 Buscando oportunidades en cripto...")
            opportunities = get_top_signals(asset_type="crypto", min_criteria=5)
            for symbol in opportunities:
                price = get_current_price(symbol)
                if not price:
                    print(f"❌ Precio no disponible para {symbol}")
                    continue  # saltar este símbolo y seguir con los demás

                place_order_with_trailing_stop(symbol, 1000, 2.0)
                pending_opportunities.add(symbol)

            log_event(f"🟡 Total invertido en este ciclo cripto: {invested_today_usd:.2f} USD")
        time.sleep(1200)

def short_scan():
    while True:
        if is_market_open() and not is_market_volatile_or_low_volume():
            print("🔍 Buscando oportunidades en corto...")
            shorts = get_top_shorts(min_criteria=5)
            for symbol in shorts:
                try:
                    asset = api.get_asset(symbol)
                    if asset.shortable:
                        place_short_order_with_trailing_buy(symbol, 1000, 2.0)
                except Exception as e:
                    print(f"❌ Error verificando shortabilidad de {symbol}: {e}")

            log_event(f"🔻 Total invertido en este ciclo de shorts: {invested_today_usd:.2f} USD")
        time.sleep(1800)

def daily_summary():
    while True:
        now = datetime.utcnow()
        if now.hour == 20:
            subject = "Resumen diario de trading 📈"
            body = "Oportunidades detectadas hoy:\n" + "\n".join(sorted(pending_opportunities))
            body += "\n\nÓrdenes ejecutadas hoy:\n" + "\n".join(sorted(pending_trades))

            try:
                positions = api.list_positions()
                total_pnl = 0
                for p in positions:
                    avg_entry = float(p.avg_entry_price)
                    current_price = float(p.current_price)
                    qty = float(p.qty)
                    total_pnl += (current_price - avg_entry) * qty
                body += f"\n\nGanancia/Pérdida no realizada actual: {total_pnl:.2f} USD"
                body += f"\nNúmero de posiciones abiertas: {len(positions)}"
            except Exception as e:
                body += f"\n\n❌ Error obteniendo PnL: {e}"

            send_email(subject, body, attach_log=True)
            pending_opportunities.clear()
            pending_trades.clear()
        time.sleep(3600)

def start_schedulers():
    threading.Thread(target=monitor_open_positions, daemon=True).start()
    threading.Thread(target=pre_market_scan, daemon=True).start()
    threading.Thread(target=crypto_scan, daemon=True).start()
    threading.Thread(target=daily_summary, daemon=True).start()
    threading.Thread(target=short_scan, daemon=True).start()

    while True:
        time.sleep(3600)

