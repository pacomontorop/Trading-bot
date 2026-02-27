"""Backtest runner — downloads historical data and runs the simulation.

Usage:
    python backtest/run.py                     # default 30 liquid symbols, 1 year
    python backtest/run.py --symbols AAPL MSFT # specific symbols
    python backtest/run.py --years 2            # longer lookback
    python backtest/run.py --universe           # sample 40 symbols from data/symbols.csv
    python backtest/run.py --csv results.csv   # save raw results to CSV

No production code is touched. Only reads config/policy.yaml.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf
import yaml

# Ensure repo root is on path (needed for config/policy.yaml path only)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine import run_symbol, TradeResult
from backtest.report import print_report

# ---------------------------------------------------------------------------
# Default symbol list — liquid large/mid caps, no API key needed
# ---------------------------------------------------------------------------
DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "TSLA",
    "JPM",  "BAC",  "GS",   "XOM",   "CVX",  "LLY",  "UNH",
    "JNJ",  "PG",   "HD",   "COST",  "WMT",  "MA",
    "V",    "ADBE", "CRM",  "ORCL",  "AMD",  "INTC", "QCOM",
    "DE",   "CAT",  "NKE",
]


def load_policy() -> dict:
    path = ROOT / "config" / "policy.yaml"
    with open(path) as f:
        return yaml.safe_load(f) or {}


def download(symbol: str, period: str) -> pd.DataFrame:
    """Download daily OHLCV from Yahoo Finance. Returns empty df on failure."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval="1d", timeout=10)
        if hist is None or hist.empty:
            return pd.DataFrame()
        hist.index = hist.index.tz_localize(None)
        return hist
    except Exception as exc:
        print(f"  [!] {symbol}: download failed — {exc}")
        return pd.DataFrame()


def sample_universe(n: int = 40) -> list[str]:
    """Return a random sample from data/symbols.csv."""
    path = ROOT / "data" / "symbols.csv"
    if not path.exists():
        print("[!] data/symbols.csv not found — using default symbols")
        return DEFAULT_SYMBOLS
    symbols: list[str] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = (row.get("Symbol") or "").strip()
            tradable = (row.get("Tradable") or "").strip().upper()
            if sym and tradable == "TRUE":
                symbols.append(sym)
    if len(symbols) <= n:
        return symbols
    return random.sample(symbols, n)


def save_csv(results: list[TradeResult], path: str) -> None:
    if not results:
        return
    with open(path, "w", newline="") as f:
        fields = list(results[0].__dataclass_fields__.keys())
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow({k: getattr(r, k) for k in fields})
    print(f"\nRaw results saved to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading-bot backtest (Phase 1)")
    parser.add_argument("--symbols", nargs="+", help="Ticker list (overrides default)")
    parser.add_argument("--years",   type=int, default=1, help="Lookback in years (default 1)")
    parser.add_argument("--universe", action="store_true", help="Sample 40 symbols from data/symbols.csv")
    parser.add_argument("--csv",  default="", help="Save raw results to this CSV file")
    args = parser.parse_args()

    # Resolve symbol list
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    elif args.universe:
        symbols = sample_universe(40)
    else:
        symbols = DEFAULT_SYMBOLS

    period = f"{args.years}y"
    policy = load_policy()

    print(f"\nBacktest — {len(symbols)} symbols, {period} lookback")
    print("Downloading SPY as benchmark...")
    spy_hist = download("SPY", period)
    if spy_hist.empty:
        print("  [!] SPY unavailable — RS vs SPY indicator will be zero")
        spy_hist = None

    all_results: list[TradeResult] = []
    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i:2d}/{len(symbols)}] {symbol}...", end=" ", flush=True)
        hist = download(symbol, period)
        if hist.empty or len(hist) < 60:
            print("skip (not enough data)")
            continue
        results = run_symbol(symbol, hist, spy_hist, policy)
        baseline_n  = sum(1 for r in results if r.strategy == "baseline")
        filtered_n  = sum(1 for r in results if r.strategy == "filtered")
        print(f"baseline={baseline_n} filtered={filtered_n} trades")
        all_results.extend(results)

    if not all_results:
        print("\nNo trades generated. Try --years 2 or different symbols.")
        return

    print_report(all_results)

    if args.csv:
        save_csv(all_results, args.csv)


if __name__ == "__main__":
    main()
