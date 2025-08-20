#scheduler.py


"""Background tasks for scanning signals and monitoring market status."""

from core.executor import (
    place_order_with_trailing_stop,
    calculate_investment_amount,
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
from core.grade_news import scan_grade_changes
from signals.filters import is_position_open, get_cached_positions

from utils.daily_risk import get_today_pnl_details
from utils.daily_set import DailySet


import threading
from datetime import datetime
from pytz import timezone
import os
import pandas as pd
import time as pytime

from signals.quiver_utils import initialize_quiver_caches, reset_daily_approvals

from core.crypto_worker import crypto_trades, crypto_trades_lock, crypto_worker
from utils.crypto_limit import get_crypto_limit

summary_lock = threading.Lock()
_last_summary_date = None

def get_ny_time():
    return datetime.now(timezone('America/New_York'))

PROGRESS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "pre_market_progress.json",
)

def pre_market_scan():
    print("🌀 pre_market_scan continuo iniciado.", flush=True)

    evaluated_symbols_today = DailySet(PROGRESS_FILE)

    while True:
        if not is_market_open():
            print("⏳ Mercado cerrado para acciones. Escaneo pausado.", flush=True)
            while not is_market_open():
                pytime.sleep(60)
            print("🔔 Mercado abierto. Reanudando escaneo de oportunidades.", flush=True)
        now_ny = get_ny_time()
        print(f"⏰ {now_ny.strftime('%Y-%m-%d %H:%M:%S')} NY | Mercado abierto", flush=True)

        # Reinicia lista si es un nuevo día
        if evaluated_symbols_today.reset_if_new_day():
            reset_daily_approvals()
            print("🔁 Nuevo día detectado, reiniciando lista de símbolos.", flush=True)

        # Obtener señales solo una vez por ciclo
        get_cached_positions(refresh=True)

        # Construir conjunto de exclusión para evitar reevaluaciones
        exclude_symbols = set(evaluated_symbols_today)
        with pending_opportunities_lock:
            exclude_symbols.update(pending_opportunities)
        with executed_symbols_today_lock:
            exclude_symbols.update(executed_symbols_today)

        evaluated_opportunities = get_top_signals(
            verbose=True, exclude=exclude_symbols
        )

        if evaluated_opportunities:
            print(f"🔎 {len(evaluated_opportunities)} oportunidades encontradas.", flush=True)
            for symb, score, origin in evaluated_opportunities:
                already_evaluated = symb in evaluated_symbols_today
                with executed_symbols_today_lock:
                    already_executed = symb in executed_symbols_today
                with pending_opportunities_lock:
                    already_pending = symb in pending_opportunities
                if already_evaluated or already_pending or already_executed:
                    motivo = (
                        "evaluado" if already_evaluated else (
                            "pendiente" if already_pending else "ejecutado"
                        )
                    )
                    print(f"⏩ {symb} ya {motivo}. Se omite.", flush=True)
                    evaluated_symbols_today.add(symb)
                    continue

                evaluated_symbols_today.add(symb)

                if is_position_open(symb):
                    print(f"📌 {symb} tiene posición abierta. Se omite.", flush=True)
                    continue

                amount_usd = calculate_investment_amount(score, symbol=symb)
                log_event(f"🟡 Ejecutando orden para {symb}")
                log_event(f"🛒 Intentando comprar {symb} por {amount_usd} USD")
                with pending_opportunities_lock:
                    pending_opportunities.add(symb)
                success = place_order_with_trailing_stop(symb, amount_usd, 1.0)
                if not success:
                    with pending_opportunities_lock:
                        pending_opportunities.discard(symb)
                    log_event(f"❌ Falló la orden para {symb}")
                else:
                    log_event(f"✅ Orden enviada para {symb}")
                pytime.sleep(1.5)  # Pequeña espera entre órdenes
        else:
            print("🔍 Sin oportunidades válidas en este ciclo.", flush=True)

        pytime.sleep(1)  # Espera mínima para no saturar el sistema


def daily_summary():
    global _last_summary_date
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
            with summary_lock:
                if _last_summary_date == now.date():
                    pytime.sleep(60)
                    continue
                _last_summary_date = now.date()

            subject = "📈 Resumen diario de trading"
            limit = get_crypto_limit()

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

            # Crypto trades executed
            with crypto_trades_lock:
                crypto_snapshot = list(crypto_trades)
            if crypto_snapshot:
                body += "\n🪙 *Órdenes cripto ejecutadas:*\n"
                for line in crypto_snapshot:
                    body += f"→ {line}\n"

            # PnL y estado de cartera
            try:
                positions = api.list_positions()
                total_pnl_equity = 0
                total_pnl_crypto = 0
                equity_positions = 0
                crypto_positions = 0
                for p in positions:
                    avg_entry = float(p.avg_entry_price)
                    current_price = float(p.current_price)
                    qty = float(p.qty)
                    pnl = (current_price - avg_entry) * qty
                    if getattr(p, "asset_class", "") == "crypto":
                        total_pnl_crypto += pnl
                        crypto_positions += 1
                    else:
                        total_pnl_equity += pnl
                        equity_positions += 1
                body += "\n" + "-" * 40 + "\n"
                body += f"💰 PnL no realizado acciones: {total_pnl_equity:.2f} USD\n"
                body += f"💰 PnL no realizado crypto: {total_pnl_crypto:.2f} USD\n"
                body += f"📌 Posiciones abiertas acciones: {equity_positions}\n"
                body += f"📌 Posiciones abiertas crypto: {crypto_positions}"
            except Exception as e:
                body += f"\n\n❌ Error obteniendo PnL: {e}"

            # PnL realizado del día
            win_syms, loss_syms, realized_total = get_today_pnl_details()
            body += f"\n💵 PnL realizado: {realized_total:.2f} USD"
            body += f"\n🏆 Operaciones ganadoras: {len(win_syms)}"
            if win_syms:
                body += f" ({', '.join(win_syms)})"
            body += f"\n💔 Operaciones perdedoras: {len(loss_syms)}"
            if loss_syms:
                body += f" ({', '.join(loss_syms)})"
            body += (
                f"\n🪙 Capital cripto usado hoy: {limit.spent:.2f} USD de "
                f"{limit.max_notional:.2f} USD"
            )

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

    # Inicializa cachés de Quiver solo cuando se lanzan los schedulers
    # para evitar llamadas de red innecesarias durante las importaciones.
    initialize_quiver_caches()

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
    threading.Thread(target=scan_grade_changes, daemon=True).start()
    threading.Thread(target=crypto_worker, daemon=True).start()

    ENABLE_SHORTS = os.getenv("ENABLE_SHORTS", "false").lower() == "true"

    if ENABLE_SHORTS:
        print("🟢 ENABLE_SHORTS=True: Activando escaneo de oportunidades short...", flush=True)
        threading.Thread(target=short_scan, daemon=True).start()
    else:
        print("🔕 Short scanning desactivado (ENABLE_SHORTS=False)", flush=True)



# Exportar para pruebas o logs manuales
if __name__ == "__main__":
    daily_summary()



