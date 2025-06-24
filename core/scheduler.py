"""Background tasks for scanning signals and monitoring market status."""

from core.executor import (
    place_order_with_trailing_stop,
    place_short_order_with_trailing_buy,
    pending_opportunities,
    pending_trades,
    pending_opportunities_lock,
    pending_trades_lock,
    invested_today_usd,
    quiver_signals_log,
    executed_symbols_today,
    executed_symbols_today_lock
)

from core.options_trader import run_options_strategy, get_options_log_and_reset
from signals.reader import get_top_signals, get_top_shorts
from broker.alpaca import api, is_market_open
from utils.emailer import send_email
from utils.logger import log_event
from core.monitor import monitor_open_positions
from utils.generate_symbols_csv import generate_symbols_csv
from signals.filters import is_position_open, get_cached_positions


import threading
from datetime import datetime
from pytz import timezone
import os
import pandas as pd
import time as pytime

from signals.quiver_utils import initialize_quiver_caches  # 👈 Añadido aquí
initialize_quiver_caches()  # 👈 Llamada a la función antes de iniciar nada más

# Flag to control short-selling features via environment variable
ENABLE_SHORTS = os.getenv("ENABLE_SHORTS", "false").lower() == "true"



def get_ny_time():
    return datetime.now(timezone('America/New_York'))


def calculate_investment_amount(score, min_score=6, max_score=19, min_investment=2000, max_investment=3000):
    if score < min_score:
        return min_investment
    normalized_score = min(max(score, min_score), max_score)
    proportion = (normalized_score - min_score) / (max_score - min_score)
    return int(min_investment + proportion * (max_investment - min_investment))

def pre_market_scan():
    print("🌀 pre_market_scan continuo iniciado.", flush=True)

    evaluated_symbols_today = set()
    last_reset_date = datetime.utcnow().date()

    while True:
        now_ny = get_ny_time()

        if is_market_open():
            # Reinicia lista si es un nuevo día
            today = now_ny.date()
            if today != last_reset_date:
                evaluated_symbols_today.clear()
                last_reset_date = today
                print("🔁 Nuevo día detectado, reiniciando lista de símbolos.", flush=True)

            # Obtener señales solo una vez por ciclo
            get_cached_positions(refresh=True)
            evaluated_opportunities = get_top_signals(verbose=True)

            if evaluated_opportunities:
                print(f"🔎 {len(evaluated_opportunities)} oportunidades encontradas.", flush=True)
                for symb, score, origin in evaluated_opportunities:
                    if symb in evaluated_symbols_today:
                        print(f"⏩ {symb} ya evaluado hoy. Se omite.", flush=True)
                        continue
                    if is_position_open(symb):
                        print(f"📌 {symb} tiene posición abierta. Se omite.", flush=True)
                        evaluated_symbols_today.add(symb)
                        continue

                    with pending_opportunities_lock:
                        already_pending = symb in pending_opportunities
                    with executed_symbols_today_lock:
                        already_executed = symb in executed_symbols_today

                    if already_pending or already_executed:
                        motivo = "pendiente" if already_pending else "ejecutado"
                        print(f"⏩ {symb} ya {motivo}. No se envía orden.", flush=True)
                        evaluated_symbols_today.add(symb)
                        continue

                    amount_usd = calculate_investment_amount(score)
                    log_event(f"🛒 Intentando comprar {symb} por {amount_usd} USD")
                    success = place_order_with_trailing_stop(symb, amount_usd, 1.5)
                    evaluated_symbols_today.add(symb)
                    if success:
                        with pending_opportunities_lock:
                            pending_opportunities.add(symb)
                    pytime.sleep(1.5)  # Pequeña espera entre órdenes
            else:
                print("🔍 Sin oportunidades válidas en este ciclo.", flush=True)


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
            with pending_opportunities_lock:
                pending_count = len(pending_opportunities)
            with pending_trades_lock:
                trades_count = len(pending_trades)
            summary_stats = (
                "📊 *Estadísticas del día:*\n"
                f"• Oportunidades detectadas: {pending_count}\n"
                f"• Órdenes ejecutadas: {trades_count}\n"
                f"• Total invertido hoy: {invested_today_usd():.2f} USD\n"
                "\n" + "-" * 40 + "\n"
            )

            # Oportunidades detectadas
            body = summary_stats
            body += "🟡 *Oportunidades detectadas:*\n"
            with pending_opportunities_lock:
                for sym in sorted(pending_opportunities):
                    body += f"→ {sym}\n"

            # Órdenes ejecutadas
            body += "\n🟢 *Órdenes ejecutadas:*\n"
            with pending_trades_lock:
                trades_snapshot = list(sorted(pending_trades))
            for trade in trades_snapshot:
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
            with pending_opportunities_lock:
                pending_opportunities.clear()
            with pending_trades_lock:
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
    if ENABLE_SHORTS:
        threading.Thread(target=short_scan, daemon=True).start()
    else:
        print("🔕 Short scanning disabled (ENABLE_SHORTS=False)", flush=True)



# Exportar para pruebas o logs manuales
if __name__ == "__main__":
    daily_summary()



