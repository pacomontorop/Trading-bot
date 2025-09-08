import os
import re
from datetime import datetime

from utils.telegram_alert import send_telegram_alert
from utils.order_tracker import compute_cumulative_stats

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "trading.log")

def _parse_today_events(log_path: str, target_date: datetime.date):
    success = failures = shorts = errors = 0
    if not os.path.exists(log_path):
        return success, failures, shorts, errors
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("["):
                continue
            try:
                ts_str, msg = line.strip().split("]", 1)
                ts_date = datetime.strptime(ts_str.lstrip("["), "%Y-%m-%d %H:%M:%S").date()
            except Exception:
                continue
            if ts_date != target_date:
                continue
            if "Short y trailing buy" in msg:
                shorts += 1
            if "✅" in msg and (
                "Orden enviada" in msg
                or "Compra y trailing stop" in msg
                or "Short y trailing buy" in msg
            ):
                success += 1
            if "Falló la orden" in msg:
                failures += 1
            if any(k in msg for k in ["❌", "⚠️", "⛔", "Error"]):
                errors += 1
    return success, failures, shorts, errors

def generate_cumulative_report(verbose: bool = False) -> None:
    today = datetime.utcnow().date()
    success, failures, shorts, errors = _parse_today_events(LOG_FILE, today)
    total_orders, wins, losses, realized = compute_cumulative_stats()

    message = (
        f"📊 Resumen del {today}\n"
        f"✅ Órdenes exitosas: {success}\n"
        f"❌ Órdenes fallidas: {failures}\n"
        f"📉 Shorts ejecutados: {shorts}\n"
        f"⚠️ Errores: {errors}\n"
        f"📦 Órdenes ejecutadas acumuladas: {total_orders}\n"
        f"💵 PnL realizado acumulado: {realized:.2f} USD\n"
        f"🏆 Operaciones ganadoras: {wins}\n"
        f"💔 Operaciones perdedoras: {losses}"
    )

    send_telegram_alert(message, verbose=verbose)
