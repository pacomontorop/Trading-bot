from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

api = None  # Will be imported lazily to avoid requiring credentials during tests

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PNL_LOG_FILE = PROJECT_ROOT / "data" / "daily_pnl_log.csv"
EQUITY_LOG_FILE = PROJECT_ROOT / "data" / "equity_log.csv"


def register_trade_pnl(symbol: str, pnl_value: float) -> None:
    """Append a trade PnL entry to ``daily_pnl_log.csv``.

    Parameters
    ----------
    symbol:
        The traded asset's ticker symbol.
    pnl_value:
        The profit or loss in USD for the trade.
    """
    PNL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_exists = PNL_LOG_FILE.exists()
    with open(PNL_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "symbol", "pnl_usd"])
        writer.writerow([datetime.utcnow().date().isoformat(), symbol, round(pnl_value, 2)])


def get_today_pnl() -> float:
    """Return the cumulative PnL for the current UTC date."""
    if not PNL_LOG_FILE.exists():
        return 0.0
    today_str = datetime.utcnow().date().isoformat()
    total = 0.0
    with open(PNL_LOG_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("date") == today_str:
                try:
                    total += float(row.get("pnl_usd", 0))
                except ValueError:
                    continue
    return total


def save_equity_snapshot() -> None:
    """Save the current equity value to ``equity_log.csv`` once per day.

    If a snapshot for the current UTC date already exists, the function does
    nothing. The CSV will be created along with its header if it does not yet
    exist.
    """
    global api
    if api is None:
        try:
            from broker.alpaca import api as live_api
            api = live_api
        except Exception:  # pragma: no cover - environment without API
            return

    EQUITY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    today_str = datetime.utcnow().date().isoformat()

    if EQUITY_LOG_FILE.exists():
        with open(EQUITY_LOG_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("date") == today_str:
                    return

    try:
        account = api.get_account()
        equity = float(getattr(account, "equity", 0))
    except Exception:
        return

    file_exists = EQUITY_LOG_FILE.exists()
    with open(EQUITY_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "equity"])
        writer.writerow([today_str, round(equity, 2)])


def is_equity_drop_exceeded(threshold_pct: float = 5.0) -> bool:
    """Return ``True`` if equity dropped more than ``threshold_pct`` from yesterday.

    Parameters
    ----------
    threshold_pct:
        Percentage drop threshold to trigger the stop.
    """
    global api
    if api is None:
        try:
            from broker.alpaca import api as live_api
            api = live_api
        except Exception:  # pragma: no cover - environment without API
            return False

    if not EQUITY_LOG_FILE.exists():
        return False

    try:
        current_equity = float(getattr(api.get_account(), "equity", 0))
    except Exception:
        return False

    yesterday_str = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    prev_equity = None
    with open(EQUITY_LOG_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("date") == yesterday_str:
                try:
                    prev_equity = float(row.get("equity", 0))
                except ValueError:
                    prev_equity = None
                break

    if prev_equity in (None, 0):
        return False

    drop_pct = (prev_equity - current_equity) / prev_equity * 100
    return drop_pct > threshold_pct

def is_risk_limit_exceeded() -> bool:
    """Check if the daily risk limit has been exceeded."""
    limit_str = os.getenv("DAILY_RISK_LIMIT")
    if not limit_str:
        return False
    try:
        limit = float(limit_str)
    except ValueError:
        return False
    if limit >= 0:
        return False
    return get_today_pnl() <= limit
