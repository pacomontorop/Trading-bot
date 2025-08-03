import os
from datetime import datetime, timedelta
from typing import Dict, Sequence

import pandas as pd

from utils.emailer import send_email


Trade = Dict[str, str]


def analyze_trades(trades: Sequence[Trade]) -> Dict[str, object]:
    total = len(trades)
    pnl_values = [float(t.get("pnl_usd", 0)) for t in trades]
    total_pnl = sum(pnl_values)
    win_rate = (sum(p > 0 for p in pnl_values) / total * 100) if total else 0.0
    average_pnl = (total_pnl / total) if total else 0.0

    signal_counts: Dict[str, int] = {}
    signal_totals: Dict[str, float] = {}
    for t in trades:
        sig = t.get("signal", "")
        pnl = float(t.get("pnl_usd", 0))
        signal_counts[sig] = signal_counts.get(sig, 0) + 1
        signal_totals[sig] = signal_totals.get(sig, 0.0) + pnl

    signals_summary = [
        {
            "signal": sig,
            "count": cnt,
            "avg_pnl": signal_totals[sig] / cnt if cnt else 0.0,
        }
        for sig, cnt in sorted(signal_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnl_values:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_dd:
            max_dd = drawdown

    symbol_totals: Dict[str, float] = {}
    for t in trades:
        sym = t.get("symbol", "")
        pnl = float(t.get("pnl_usd", 0))
        symbol_totals[sym] = symbol_totals.get(sym, 0.0) + pnl
    sorted_symbols_desc = sorted(symbol_totals.items(), key=lambda kv: kv[1], reverse=True)
    top_symbols = sorted_symbols_desc[:5]
    sorted_symbols_asc = sorted(symbol_totals.items(), key=lambda kv: kv[1])
    bottom_symbols = sorted_symbols_asc[:5]

    return {
        "total_trades": total,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "average_pnl": average_pnl,
        "signals_summary": signals_summary,
        "max_drawdown": max_dd,
        "top_symbols": top_symbols,
        "bottom_symbols": bottom_symbols,
    }


def format_summary(stats: Dict[str, object]) -> str:
    lines = [
        f"Total de operaciones: {stats['total_trades']}",
        f"PnL total: {stats['total_pnl']:.2f} USD",
        f"Win rate: {stats['win_rate']:.2f}%",
        f"PnL medio por operación: {stats['average_pnl']:.2f} USD",
    ]
    if stats["signals_summary"]:
        lines.append("Señales más frecuentes y PnL medio:")
        for entry in stats["signals_summary"]:
            lines.append(
                f" - {entry['signal']}: {entry['count']} ops, avg {entry['avg_pnl']:.2f}"
            )
    lines.append(f"Máximo drawdown: {stats['max_drawdown']:.2f} USD")
    if stats["top_symbols"]:
        lines.append("5 símbolos más rentables:")
        for sym, pnl in stats["top_symbols"]:
            lines.append(f" - {sym}: {pnl:.2f}")
    if stats["bottom_symbols"]:
        lines.append("5 símbolos menos rentables:")
        for sym, pnl in stats["bottom_symbols"]:
            lines.append(f" - {sym}: {pnl:.2f}")
    return "\n".join(lines)


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
