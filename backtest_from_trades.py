#backtest_from_trades.py

#!/usr/bin/env python3
"""Analyze historical trades from a CSV file."""

import argparse
import csv
import os
from typing import Dict, List, Sequence, Tuple
import statistics
import math


Trade = Dict[str, str]


def read_trades(path: str) -> List[Trade]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def analyze_trades(trades: Sequence[Trade]) -> Dict[str, object]:
    total = len(trades)
    pnl_values = [float(t.get("pnl_usd", 0)) for t in trades]
    total_pnl = sum(pnl_values)
    win_rate = (sum(p > 0 for p in pnl_values) / total * 100) if total else 0.0
    average_pnl = (total_pnl / total) if total else 0.0

    # Signals
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

    # Drawdown calculation
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

    # Sharpe ratio (risk-free rate assumed 0). Use population stdev for stability.
    sharpe = 0.0
    if pnl_values:
        mean_pnl = statistics.mean(pnl_values)
        stdev = statistics.pstdev(pnl_values)
        if stdev > 0:
            sharpe = mean_pnl / stdev * math.sqrt(len(pnl_values))

    # Symbols profitability
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
        "sharpe_ratio": sharpe,
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
    lines.append(f"Sharpe ratio: {stats['sharpe_ratio']:.2f}")
    if stats["top_symbols"]:
        lines.append("5 símbolos más rentables:")
        for sym, pnl in stats["top_symbols"]:
            lines.append(f" - {sym}: {pnl:.2f}")
    if stats["bottom_symbols"]:
        lines.append("5 símbolos menos rentables:")
        for sym, pnl in stats["bottom_symbols"]:
            lines.append(f" - {sym}: {pnl:.2f}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest summary from trades.csv")
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save summary to data/backtest_summary.txt",
    )
    args = parser.parse_args()

    csv_path = os.path.join("data", "trades.csv")
    trades = read_trades(csv_path)
    stats = analyze_trades(trades)
    summary = format_summary(stats)
    print(summary)

    if args.save:
        out_path = os.path.join("data", "backtest_summary.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(summary)


if __name__ == "__main__":
    main()
