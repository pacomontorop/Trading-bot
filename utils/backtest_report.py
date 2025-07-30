import os
from datetime import datetime, timedelta
import pandas as pd

from utils.emailer import send_email


def generate_paper_summary():
    """Generate a weekly summary of paper trades and email it."""
    trades_path = os.path.join("data", "trades.csv")
    if not os.path.exists(trades_path):
        return None

    try:
        df = pd.read_csv(trades_path)
    except Exception:
        return None

    if df.empty:
        return None

    # Determine timestamp column name
    ts_col = None
    for col in ["timestamp", "date", "time", "datetime"]:
        if col in df.columns:
            ts_col = col
            break
    if ts_col is None:
        ts_col = df.columns[0]

    df[ts_col] = pd.to_datetime(df[ts_col])
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent = df[df[ts_col] >= cutoff]

    if recent.empty:
        return None

    total_ops = len(recent)
    pnl_col = "pnl" if "pnl" in recent.columns else None
    if pnl_col:
        total_pnl = recent[pnl_col].sum()
        winners = recent[recent[pnl_col] > 0]
        win_rate = len(winners) / total_ops if total_ops else 0
        cumulative = recent[pnl_col].cumsum()
        running_max = cumulative.cummax()
        drawdown = (running_max - cumulative).max()
    else:
        total_pnl = 0.0
        win_rate = 0.0
        drawdown = 0.0

    signal_col = None
    for col in ["signal", "strategy", "reason"]:
        if col in recent.columns:
            signal_col = col
            break

    signals_summary = ""
    if signal_col:
        freq = recent[signal_col].value_counts().head(3)
        lines = []
        for sig, count in freq.items():
            if pnl_col:
                avg = recent[recent[signal_col] == sig][pnl_col].mean()
            else:
                avg = 0.0
            lines.append(f"{sig}: {avg:.2f} USD ({count})")
        signals_summary = "\n".join(lines)

    summary = (
        f"📊 Resumen de la última semana:\n"
        f"• Operaciones totales: {total_ops}\n"
        f"• Win rate: {win_rate*100:.2f}%\n"
        f"• PnL total: {total_pnl:.2f} USD\n"
        f"• Drawdown máximo: {drawdown:.2f} USD"
    )
    if signals_summary:
        summary += "\n\n📈 Señales frecuentes:\n" + signals_summary

    send_email("📄 Resumen semanal paper trading", summary)
    return summary
