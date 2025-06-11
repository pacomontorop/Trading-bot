#schedulers.py

from core.executor import (
    place_order_with_trailing_stop,
    place_short_order_with_trailing_buy,
    pending_opportunities,
    pending_trades,
    invested_today_usd,
    quiver_signals_log
)

from core.options_trader import run_options_strategy, get_options_log_and_reset
from broker.alpaca import api, get_current_price, is_market_open
from signals.reader import get_top_signals, get_top_shorts
from utils.emailer import send_email
from utils.logger import log_event
from core.monitor import monitor_open_positions
from utils.generate_symbols_csv import generate_symbols_csv
from signals.filters import is_position_open


from signals.quiver_utils import initialize_quiver_caches  # 👈 Añadido aquí
initialize_quiver_caches()  # 👈 Llamada a la función antes de iniciar nada más

import threading
from datetime import datetime, time
from pytz import timezone
import os
import pandas as pd
import time as pytime



def get_ny_time():
    return datetime.now(timezone('America/New_York'))

def is_market_open(now_ny=None):
    if not now_ny:
        now_ny = get_ny_time()
    return (
        now_ny.weekday() < 5 and
        time(9, 30) <= now_ny.time() <= time(16, 0)
    )

def calculate_investment_amount(score, min_score=6, max_score=19, min_investment=2000, max_investment=3000):
    if score < min_score:
        return min_investment
    normalized_score = min(max(score, min_score), max_score)
    proportion = (normalized_score - min_score) / (max_score - min_score)
    return int(min_investment + proportion * (max_investment - min_investment))

def pre_market_scan():
    print("🌀 pre_market_scan continuo iniciado.", flush=True)

    from signals.reader import stock_assets  # <- Lista de símbolos ya barajada y con prioridad

    evaluated_symbols_today = set()
    last_reset_date = datetime.utcnow().date()

    while True:
        now_ny = get_ny_time()

        if is_market_open(now_ny):
            # Reinicia lista si es un nuevo día
            today = now_ny.date()
            if today != last_reset_date:
                evaluated_symbols_today.clear()
                last_reset_date = today
                print("🔁 Nuevo día detectado, reiniciando lista de símbolos.", flush=True)

            # Buscar el siguiente símbolo a evaluar
            for symbol in stock_assets:
                if symbol in evaluated_symbols_today or is_position_open(symbol):
                    continue

                evaluated_symbols_today.add(symbol)

                # ⚡️ Evaluación completa
                print(f"🔍 Evaluando {symbol}...", flush=True)
                evaluated_opportunities = get_top_signals(min_criteria=6, verbose=True)

                if evaluated_opportunities:
                    for symb, score, origin in evaluated_opportunities:
                        if is_position_open(symb) or symb in pending_opportunities:
                            continue
                        amount_usd = calculate_investment_amount(score)
                        place_order_with_trailing_stop(symb, amount_usd, 1.5)
                        pending_opportunities.add(symb)
                        pytime.sleep(1.5)  # Pequeña espera entre órdenes

                break  # ⬅️ Sal de for para que vuelva a empezar el while sin terminar toda la lista

        else:
            print("⏳ Mercado cerrado para acciones.", flush=True)
            pytime.sleep(60)  # Espera 1 min cuando está cerrado

        pytime.sleep(1)  # Espera mínima para no saturar el sistema




def short_scan():
    print("🌀 short_scan iniciado.", flush=True)
    while True:
        if is_market_open():
            print("🔍 Buscando oportunidades en corto...", flush=True)
            shorts = get_top_shorts(min_criteria=6, verbose=True)
            log_event(f"🔻 {len(shorts)} oportunidades encontradas para short (máx 5 por ciclo)")
            MAX_SHORTS_PER_CYCLE = 1

            if len(shorts) > MAX_SHORTS_PER_CYCLE:
                print(f"⚠️ Hay más de {MAX_SHORTS_PER_CYCLE} shorts válidos. Se ejecutan solo las primeras.")

            for symbol, score, origin in shorts[:MAX_SHORTS_PER_CYCLE]:
                try:
                    asset = api.get_asset(symbol)
                    if asset.shortable:
                        amount_usd = calculate_investment_amount(score)
                        place_short_order_with_trailing_buy(symbol, amount_usd, 1.5)
                except Exception as e:
                    print(f"❌ Error verificando shortabilidad de {symbol}: {e}", flush=True)

            log_event(f"🔻 Total invertido en este ciclo de shorts: {invested_today_usd():.2f} USD")
        pytime.sleep(300)

def daily_summary():
    print("🌀 daily_summary iniciado.", flush=True)
    while True:
        now = datetime.utcnow()
        if now.hour == 20:
            subject = "📈 Resumen diario de trading"

            # Cabecera numérica
            summary_stats = (
                "📊 *Estadísticas del día:*\n"
                f"• Oportunidades detectadas: {len(pending_opportunities)}\n"
                f"• Órdenes ejecutadas: {len(pending_trades)}\n"
                f"• Total invertido hoy: {invested_today_usd():.2f} USD\n"
                "\n" + "-" * 40 + "\n"
            )

            # Oportunidades detectadas
            body = summary_stats
            body += "🟡 *Oportunidades detectadas:*\n"
            for sym in sorted(pending_opportunities):
                body += f"→ {sym}\n"

            # Órdenes ejecutadas
            body += "\n🟢 *Órdenes ejecutadas:*\n"
            for trade in sorted(pending_trades):
                symbol = trade.split()[0].replace("SHORT:", "").strip(":")
                signals = quiver_signals_log.get(symbol, [])
                amount_usd = trade.split("$")[-1] if "$" in trade else ""

                # Composición elegante de la línea de salida
                tipo = "SHORT" if "SHORT" in trade else "LONG"
                line = f"{symbol} [{tipo}] — {amount_usd} USD"
                if signals:
                    line += f" — 🧠 Señales: {', '.join(signals)}"
                body += f"→ {line}\n"

            # PnL y estado de cartera
            try:
                positions = api.list_positions()
                total_pnl = 0
                for p in positions:
                    avg_entry = float(p.avg_entry_price)
                    current_price = float(p.current_price)
                    qty = float(p.qty)
                    total_pnl += (current_price - avg_entry) * qty
                body += "\n" + "-" * 40 + "\n"
                body += f"💰 PnL no realizado actual: {total_pnl:.2f} USD\n"
                body += f"📌 Posiciones abiertas: {len(positions)}"
            except Exception as e:
                body += f"\n\n❌ Error obteniendo PnL: {e}"

            # Opciones
            options_log = get_options_log_and_reset()
            if options_log:
                body += "\n\n📘 *Operaciones con opciones:*\n"
                body += "\n".join(f"→ {line}" for line in options_log)

            # Envío y limpieza
            send_email(subject, body, attach_log=True)
            pending_opportunities.clear()
            pending_trades.clear()

        pytime.sleep(3600)

        

def start_schedulers():
    print("🟢 Iniciando verificación de symbols.csv...", flush=True)
    regenerate = True

    try:
        if os.path.exists("data/symbols.csv"):
            df = pd.read_csv("data/symbols.csv", on_bad_lines='skip')
            if not df.empty and "Symbol" in df.columns:
                regenerate = False
                print(f"✅ symbols.csv ya existe con {len(df)} símbolos. No se regenera.", flush=True)
            else:
                print("⚠️ symbols.csv está vacío o incompleto. Se regenerará.", flush=True)
        else:
            print("📂 symbols.csv no existe. Se generará.", flush=True)
    except Exception as e:
        print(f"❌ Error al verificar symbols.csv: {e}. Se generará igualmente.", flush=True)

    if regenerate:
        try:
            generate_symbols_csv()
            print("✅ symbols.csv generado correctamente.", flush=True)
        except Exception as e:
            print(f"❌ Error al generar symbols.csv: {e}", flush=True)

    print("🟢 Lanzando schedulers...", flush=True)
    threading.Thread(target=monitor_open_positions, daemon=True).start()
    threading.Thread(target=pre_market_scan, daemon=True).start()
    threading.Thread(target=daily_summary, daemon=True).start()
    threading.Thread(target=short_scan, daemon=True).start()



# Exportar para pruebas o logs manuales
if __name__ == "__main__":
    daily_summary()



