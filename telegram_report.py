import os
from collections import defaultdict
from dotenv import load_dotenv
import requests

from utils.logger import log_dir
from broker.alpaca import api, get_current_price
from core.executor import quiver_signals_log, pending_trades, entry_data


def _parse_pnl_log(path: str):
    """Parse all realized PnL entries from ``pnl.log``."""
    trades = []
    if not os.path.exists(path):
        return trades
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            if line.startswith('[') and ']' in line:
                _, remainder = line.split(']', 1)
            else:
                remainder = line
            parts = remainder.strip().split()
            if len(parts) >= 2:
                symbol = parts[0]
                try:
                    pnl = float(parts[-1])
                except ValueError:
                    continue
                trades.append((symbol, pnl))
    return trades


def generate_cumulative_report() -> bool:
    """Generate cumulative trading report and send via Telegram."""
    load_dotenv()
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')

    pnl_path = os.path.join(log_dir, 'pnl.log')
    trades = _parse_pnl_log(pnl_path)
    total_realized = sum(p for _, p in trades)
    completed = len(trades)

    wins = [p for _, p in trades if p > 0]
    win_rate = (len(wins) / completed * 100) if completed else 0.0
    average_pnl = (total_realized / completed) if completed else 0.0

    best_trade = max(trades, key=lambda t: t[1], default=(None, 0.0))
    worst_trade = min(trades, key=lambda t: t[1], default=(None, 0.0))

    unrealized = 0.0
    try:
        positions = api.list_positions()
        for p in positions:
            avg_entry = float(p.avg_entry_price)
            current_price = float(p.current_price)
            qty = float(p.qty)
            unrealized += (current_price - avg_entry) * qty
    except Exception:
        positions = []

    for symbol, (entry_price, qty, _) in entry_data.items():
        try:
            current_price = next((float(p.current_price) for p in positions if p.symbol == symbol), None)
            if current_price is None:
                current_price = float(get_current_price(symbol) or 0)
            unrealized += (current_price - float(entry_price)) * float(qty)
        except Exception:
            pass

    long_ops = sum(1 for t in pending_trades if "SHORT" not in t)
    short_ops = sum(1 for t in pending_trades if "SHORT" in t)

    signal_counts = defaultdict(int)
    signal_pnls = defaultdict(float)
    for symbol, pnl in trades:
        for sig in quiver_signals_log.get(symbol, []):
            signal_counts[sig] += 1
            signal_pnls[sig] += pnl

    top_signals = sorted(signal_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    signal_lines = []
    for sig, count in top_signals:
        avg = signal_pnls[sig] / count if count else 0.0
        signal_lines.append(f"• {sig} ({count}) — {avg:.2f} USD")

    lines = [
        f"📊 Total de operaciones completadas: {completed}",
        f"💵 PnL realizado total: {total_realized:.2f} USD",
        f"📈 PnL no realizado: {unrealized:.2f} USD",
        f"📃 Operaciones LONG: {long_ops} | SHORT: {short_ops}",
        f"🏆 Win rate: {win_rate:.2f}%",
        f"📊 PnL medio por operación: {average_pnl:.2f} USD",
        f"🥇 Operación más rentable: {best_trade[0]} {best_trade[1]:.2f} USD",
        f"🥈 Operación menos rentable: {worst_trade[0]} {worst_trade[1]:.2f} USD",
        "🧠 Señales más frecuentes:",
        *(signal_lines or ["(sin datos)"])
    ]
    report = "\n".join(lines)

    os.makedirs('data', exist_ok=True)
    local_path = os.path.join('data', 'telegram_report_friday.txt')
    with open(local_path, 'w', encoding='utf-8') as f:
        f.write(report)

    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": report, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception:
        return False

