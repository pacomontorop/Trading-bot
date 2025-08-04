#scheduler.py


"""Background tasks for scanning signals and monitoring market status."""

from core.executor import (
    place_order_with_trailing_stop,
    pending_opportunities,
    pending_trades,
    pending_opportunities_lock,
    pending_trades_lock,
    invested_today_usd,
    quiver_signals_log,
    executed_symbols_today,
    executed_symbols_today_lock,
    short_scan,
)

from core.options_trader import run_options_strategy, get_options_log_and_reset
from signals.reader import get_top_signals
from broker.alpaca import api, is_market_open
from utils.emailer import send_email
from utils.backtest_report import generate_paper_summary, analyze_trades, format_summary
from utils.logger import log_event, log_dir
from utils.telegram_report import generate_cumulative_report
from core.monitor import monitor_open_positions, watchdog_trailing_stop
from utils.generate_symbols_csv import generate_symbols_csv
from signals.filters import is_position_open, get_cached_positions


import threading
from datetime import datetime
from pytz import timezone
import os
import pandas as pd
import time as pytime
import re

from signals.quiver_utils import initialize_quiver_caches, reset_daily_approvals  # 👈 Añadido aquí
initialize_quiver_caches()  # 👈 Llamada a la función antes de iniciar nada más

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
        market_open = is_market_open()
        print(
            f"\u23F0 {now_ny.strftime('%Y-%m-%d %H:%M:%S')} NY | is_market_open={market_open}",
            flush=True,
        )

        if market_open:
            # Reinicia lista si es un nuevo día
            today = now_ny.date()
            if today != last_reset_date:
                evaluated_symbols_today.clear()
                reset_daily_approvals()
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
                    log_event(f"🟡 Ejecutando orden para {symb}")
                    log_event(f"🛒 Intentando comprar {symb} por {amount_usd} USD")
                    success = place_order_with_trailing_stop(symb, amount_usd, 1.5)
                    if success:
                        log_event(f"✅ Orden enviada para {symb}")
                    else:
                        log_event(f"❌ Falló la orden para {symb}")
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


def _parse_today_pnl(log_path: str):
    """Parse today's realized PnL entries from ``pnl.log``."""
    wins = 0
    losses = 0
    total = 0.0
    today = datetime.utcnow().date()

    if not os.path.exists(log_path):
        return wins, losses, total

    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            # Attempt to parse optional leading timestamp in [YYYY-MM-DD HH:MM:SS]
            if line.startswith("[") and "]" in line:
                ts_str, remainder = line.split("]", 1)
                ts_str = ts_str.lstrip("[")
                try:
                    if datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").date() != today:
                        continue
                except Exception:
                    remainder = line
                line = remainder

            match = re.search(r"(-?\d+(?:\.\d+)?)", line)
            if match:
                value = float(match.group(1))
                total += value
                if value >= 0:
                    wins += 1
                else:
                    losses += 1

    return wins, losses, total

def daily_summary():
    print("🌀 daily_summary iniciado.", flush=True)
    while True:
        # Utilizar hora de Nueva York para sincronizar con el cierre del mercado
        now = get_ny_time()
        if now.weekday() == 6 and now.hour == 18:
            try:
                generate_paper_summary()
            except Exception as e:
                log_event(f"❌ Error al generar resumen semanal: {e}")

        # Enviar el resumen diario al cierre regular del mercado (16:00 NY)
        if now.hour == 16:
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

            # PnL realizado del día
            pnl_path = os.path.join(log_dir, "pnl.log")
            wins, losses, realized_total = _parse_today_pnl(pnl_path)
            body += f"\n💵 PnL realizado: {realized_total:.2f} USD"
            body += f"\n🏆 Operaciones ganadoras: {wins}"
            body += f"\n💔 Operaciones perdedoras: {losses}"

            # Opciones
            options_log = get_options_log_and_reset()
            if options_log:
                body += "\n\n📘 *Operaciones con opciones:*\n"
                body += "\n".join(f"→ {line}" for line in options_log)

            # Envío y limpieza
            try:
                trades_path = os.path.join("data", "trades.csv")
                if os.path.exists(trades_path):
                    trades = []
                    with open(trades_path, "r", encoding="utf-8") as f:
                        import csv
                        trades = list(csv.DictReader(f))
                    stats = analyze_trades(trades)
                    acumulado = format_summary(stats)
                    body += "\n\n📘 *Resumen acumulado de rentabilidad:*\n"
                    body += acumulado
                else:
                    body += "\n\n📘 *Resumen acumulado:* archivo de trades no encontrado."
            except Exception as e:
                body += f"\n\n❌ Error al calcular resumen acumulado: {e}"

            send_email(subject, body, attach_log=True)
            try:
                generate_cumulative_report()
            except Exception:
                log_event("❌ Fallo Telegram, sistema sigue OK")
            with pending_opportunities_lock:
                pending_opportunities.clear()
            with pending_trades_lock:
                pending_trades.clear()
            for fname in ("events.log", "pnl.log"):
                path = os.path.join(log_dir, fname)
                if os.path.exists(path):
                    open(path, "w").close()

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
    threading.Thread(target=watchdog_trailing_stop, daemon=True).start()
    threading.Thread(target=pre_market_scan, daemon=True).start()
    threading.Thread(target=daily_summary, daemon=True).start()

    ENABLE_SHORTS = os.getenv("ENABLE_SHORTS", "false").lower() == "true"

    if ENABLE_SHORTS:
        print("🟢 ENABLE_SHORTS=True: Activando escaneo de oportunidades short...", flush=True)
        threading.Thread(target=short_scan, daemon=True).start()
    else:
        print("🔕 Short scanning desactivado (ENABLE_SHORTS=False)", flush=True)



# Exportar para pruebas o logs manuales
if __name__ == "__main__":
    daily_summary()



