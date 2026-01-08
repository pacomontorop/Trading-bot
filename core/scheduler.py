"""Minimal long-only equity scheduler loop."""

from __future__ import annotations

import csv
import os
import time

import config
from core.executor import place_long_order
from core import risk_manager
from core.market_gate import is_us_equity_market_open
from signals.filters import is_position_open
from signals.reader import get_top_signals
from utils.generate_symbols_csv import generate_symbols_csv
from utils.logger import log_event


SYMBOLS_PATH = os.path.join("data", "symbols.csv")


def _symbols_csv_valid(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if "Symbol" not in (reader.fieldnames or []):
                return False
            for _ in reader:
                return True
    except Exception:
        return False
    return False


def _ensure_symbols_csv() -> None:
    if _symbols_csv_valid(SYMBOLS_PATH):
        log_event("SCAN symbols.csv present, using existing universe", event="SCAN")
        return
    log_event("SCAN symbols.csv missing or invalid, regenerating", event="SCAN")
    generate_symbols_csv()


def equity_scheduler_loop(interval_sec: int = 60, max_symbols: int = 30) -> None:
    """Run the single equity scheduler loop."""

    _ensure_symbols_csv()
    log_event("Scheduler loop started (equities, long-only)", event="SCAN")

    while True:
        market_open = is_us_equity_market_open()
        log_event(f"Market gate check open={market_open}", event="GATE")
        if not market_open:
            log_event("SCAN skipped reason=market_closed", event="SCAN")
            time.sleep(interval_sec)
            continue

        opportunities = get_top_signals(max_symbols=max_symbols)
        if not opportunities:
            log_event("SCAN end: no approved signals", event="SCAN")
            time.sleep(interval_sec)
            continue

        for symbol, total_score, quiver_score, price, atr, plan in opportunities:
            if is_position_open(symbol):
                log_event(
                    f"APPROVAL {symbol}: rejected reason=position_open",
                    event="APPROVAL",
                )
                continue

            log_event(
                (
                    f"ORDER {symbol}: attempt score={total_score:.2f} "
                    f"quiver={quiver_score:.2f} "
                    f"qty={plan.get('qty')} notional={plan.get('notional'):.2f}"
                ),
                event="ORDER",
            )
            success = place_long_order(plan, dry_run=config.DRY_RUN)
            if success:
                log_event(f"ORDER {symbol}: submitted", event="ORDER")
                if not config.DRY_RUN:
                    risk_manager.record_trade(plan)
            else:
                log_event(f"ORDER {symbol}: failed", event="ORDER")

        time.sleep(interval_sec)
