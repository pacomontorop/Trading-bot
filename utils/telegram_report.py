import os
import re
from datetime import datetime

from utils.telegram_alert import send_telegram_alert

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "events.log")
PNL_FILE = os.path.join(PROJECT_ROOT, "logs", "pnl.log")

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

def _parse_today_pnl(log_path: str, target_date: datetime.date):
    wins = losses = 0
    total = 0.0
    if not os.path.exists(log_path):
        return wins, losses, total
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            if line.startswith("[") and "]" in line:
                ts_str, remainder = line.split("]", 1)
                try:
                    ts_date = datetime.strptime(ts_str.lstrip("["), "%Y-%m-%d %H:%M:%S").date()
                    if ts_date != target_date:
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

def generate_cumulative_report(verbose: bool = False) -> None:
    today = datetime.utcnow().date()
    success, failures, shorts, errors = _parse_today_events(LOG_FILE, today)
    wins, losses, realized = _parse_today_pnl(PNL_FILE, today)

    message = (
        f"📊 Resumen del {today}\n"
        f"✅ Órdenes exitosas: {success}\n"
        f"❌ Órdenes fallidas: {failures}\n"
        f"📉 Shorts ejecutados: {shorts}\n"
        f"⚠️ Errores: {errors}\n"
        f"💵 PnL realizado: {realized:.2f} USD\n"
        f"🏆 Ganadoras: {wins} | 💔 Perdedoras: {losses}"
    )

    send_telegram_alert(message, verbose=verbose)
