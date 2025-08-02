from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PNL_LOG_FILE = PROJECT_ROOT / "data" / "daily_pnl_log.csv"


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
