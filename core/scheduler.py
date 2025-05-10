import threading
from datetime import datetime
from pytz import timezone
from datetime import time
import time as pytime

from core.executor import (
    place_order_with_trailing_stop,
    place_short_order_with_trailing_buy,
    pending_opportunities,
    pending_trades,
    invested_today_usd
)
from core.options_trader import run_options_strategy, get_options_log_and_reset
from broker.alpaca import api, get_current_price, is_market_open
from signals.reader import get_top_signals, get_top_shorts
from utils.emailer import send_email
from utils.logger import log_event
from core.monitor import monitor_open_positions

def get_ny_time():
    return datetime.now(timezone('America/New_York'))

def is_market_open(now_ny=None):
    if not now_ny:
        now_ny = get_ny_time()
    return (
        now_ny.weekday() < 5 and
        time(9, 30) <= now_ny.time() <= time(16, 0)
    )

def pre_market_scan():
    print("🌀 pre_market_scan iniciado.", flush=True)

    while True:
        now_ny = get_ny_time()
        current_hour = now_ny.hour

        if is_market_open(now_ny):
            if now_ny.time() < time(9, 30):
                print("⏳ Mercado abrirá pronto...", flush=True)
            else:
                print("🔍 Buscando oportunidades en acciones...", flush=True)
                opportunities = get_top_signals(min_criteria=6, verbose=True)
                for symbol in opportunities:
                    place_order_with_trailing_stop(symbol, 500, 1.5)
                    pending_opportunities.add(symbol)

                print("📊 Ejecutando estrategia de opciones...", flush=True)
                run_options_strategy()
        else:
            print("⏳ Mercado cerrado para acciones.", flush=True)

        log_event(f"🟢 Total invertido en este ciclo de compra long: {invested_today_usd():.2f} USD")

        if 9 <= current_hour < 11 or 15 <= current_hour < 18:
            pytime.sleep(300)
        elif 11 <= current_hour < 15:
            pytime.sleep(600)
        else:
            pytime.sleep(1800)

def short_scan():
    print("🌀 short_scan iniciado.", flush=True)
    while True:
        if is_market_open():
            print("🔍 Buscando oportunidades en corto...", flush=True)
            shorts = get_top_shorts(min_criteria=6)
            for symbol in shorts:
                try:
                    asset = api.get_asset(symbol)
                    if asset.shortable:
                        place_short_order_with_trailing_buy(symbol, 500, 1.5)
                except Exception as e:
                    print(f"❌ Error verificando shortabilidad de {symbol}: {e}", flush=True)
            log_event(f"🔻 Total invertido en este ciclo de shorts: {invested_today_usd():.2f} USD")
        pytime.sleep(300)

def daily_summary():
    print("🌀 daily_summary iniciado.", flush=True)
    while True:
        now = datetime.utcnow()
        if now.hour == 20:
            subject = "Resumen diario de trading 📈"

            summary_stats = (
                f"Resumen del día 📊:\n"
                f"• Oportunidades detectadas: {len(pending_opportunities)}\n"
                f"• Órdenes ejecutadas: {len(pending_trades)}\n"
                f"• Total invertido hoy: {invested_today_usd():.2f} USD\n"
            )

            body = summary_stats
            body += "\nOportunidades detectadas hoy:\n" + "\n".join(sorted(pending_opportunities))
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

            # Agregar resumen de opciones
            options_log = get_options_log_and_reset()
            if options_log:
                body += "\n\n📘 Operaciones de opciones hoy:\n" + "\n".join(options_log)

            send_email(subject, body, attach_log=True)
            pending_opportunities.clear()
            pending_trades.clear()
        pytime.sleep(3600)

def start_schedulers():
    print("🟢 Lanzando schedulers...", flush=True)
    threading.Thread(target=monitor_open_positions, daemon=True).start()
    threading.Thread(target=pre_market_scan, daemon=True).start()
    threading.Thread(target=daily_summary, daemon=True).start()
    threading.Thread(target=short_scan, daemon=True).start()

