# scheduler.py

"""Minimal long-only equity scheduler loop."""

from __future__ import annotations

import csv
import os
import threading
import time

import config
from broker.account import get_account_equity_safe
from core.executor import (
    _apply_event_and_cutoff_policies,
    _cfg_risk,
    _equity_guard,
    calculate_position_size_risk_based,
    evaluated_longs_today,
    executed_symbols_today,
    executed_symbols_today_lock,
    get_market_exposure_factor,
    pending_opportunities,
    pending_opportunities_lock,
    place_long_order,
    reset_daily_investment,
)
from core.market_gate import is_us_equity_market_open
from signals.reader import get_top_signals, reset_symbol_rotation
from utils.generate_symbols_csv import generate_symbols_csv
from utils.logger import log_event


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
    path = os.path.join("data", "symbols.csv")
    if _symbols_csv_valid(path):
        log_event("SCAN symbols.csv present, using existing universe", event="SCAN")
        return
    log_event("SCAN symbols.csv missing or invalid, regenerating", event="SCAN")
    generate_symbols_csv()


def equity_scheduler_loop(
    interval_sec: int = 60,
    max_signals: int = 5,
    scan_batch: int = 50,
) -> None:
    log_event("Scheduler loop started (equities, long-only)", event="SCAN")
    while True:
        reset_daily_investment()
        market_open = is_us_equity_market_open()
        log_event(
            f"Market gate check open={market_open}",
            event="GATE",
        )
        if not market_open:
            log_event("SCAN skipped reason=market_closed", event="SCAN")
            time.sleep(interval_sec)
            continue

        if evaluated_longs_today.reset_if_new_day():
            reset_symbol_rotation()
            log_event("SCAN new day detected: reset evaluated symbols", event="SCAN")

        exclude_symbols = set(evaluated_longs_today)
        with pending_opportunities_lock:
            exclude_symbols.update(pending_opportunities)
        with executed_symbols_today_lock:
            exclude_symbols.update(executed_symbols_today)

        log_event(
            f"SCAN start batch_size={scan_batch} exclude={len(exclude_symbols)}",
            event="SCAN",
        )
        opportunities = get_top_signals(
            verbose=True, exclude=exclude_symbols, max_symbols=scan_batch
        )
        if not opportunities:
            log_event("SCAN end: no approved signals", event="SCAN")
            time.sleep(interval_sec)
            continue

        log_event(
            f"SCAN end: approved={len(opportunities)} (max_orders={max_signals})",
            event="SCAN",
        )

        for symbol, score, origin, current_price, current_atr in opportunities[:max_signals]:
            evaluated_longs_today.add(symbol)

            if not current_price or current_price <= 0:
                log_event(
                    f"APPROVAL {symbol}: rejected reason=invalid_price",
                    event="APPROVAL",
                )
                continue
            if current_atr is None or current_atr <= 0:
                log_event(
                    f"APPROVAL {symbol}: rejected reason=invalid_atr",
                    event="APPROVAL",
                )
                continue

            equity = get_account_equity_safe()
            if not _equity_guard(equity, config._policy, "equity_scheduler"):
                continue

            exposure = get_market_exposure_factor(config._policy)
            sizing = calculate_position_size_risk_based(
                symbol=symbol,
                price=current_price,
                atr=current_atr,
                equity=equity,
                cfg=config._policy,
                market_exposure_factor=exposure,
            )
            if sizing["shares"] <= 0 or sizing["notional"] <= 0:
                log_event(
                    f"SIZE {symbol}: rejected reason={sizing['reason']}",
                    event="ORDER",
                )
                continue

            allowed, adj_notional, reason = _apply_event_and_cutoff_policies(
                symbol, sizing["notional"], config._policy
            )
            if not allowed or adj_notional <= 0:
                log_event(
                    f"ENTRY {symbol}: rejected reason={reason}",
                    event="ORDER",
                )
                continue
            if adj_notional != sizing["notional"]:
                allow_frac = _cfg_risk(config._policy)["allow_fractional"]
                if allow_frac:
                    new_shares = adj_notional / current_price
                else:
                    new_shares = int(adj_notional // current_price)
                if new_shares <= 0:
                    log_event(
                        f"ENTRY {symbol}: rejected reason=adjusted_size_zero",
                        event="ORDER",
                    )
                    continue
                sizing["shares"] = new_shares
                sizing["notional"] = new_shares * current_price
                log_event(
                    (
                        f"ENTRY {symbol}: adjusted_size reason={reason} "
                        f"shares={new_shares:.4f} notional=${sizing['notional']:.2f}"
                    ),
                    event="ORDER",
                )

            log_event(
                (
                    f"ORDER {symbol}: placing long order "
                    f"shares={sizing['shares']:.4f} notional=${sizing['notional']:.2f} "
                    f"score={score} origin={origin}"
                ),
                event="ORDER",
            )
            with pending_opportunities_lock:
                pending_opportunities.add(symbol)
            success = place_long_order(symbol, sizing, cfg=config._policy)
            if not success:
                with pending_opportunities_lock:
                    pending_opportunities.discard(symbol)
                log_event(f"ORDER {symbol}: failed", event="ORDER")
            else:
                log_event(f"ORDER {symbol}: submitted", event="ORDER")
            time.sleep(1)

        time.sleep(interval_sec)


def start_equity_scheduler() -> None:
    _ensure_symbols_csv()
    thread = threading.Thread(target=equity_scheduler_loop, daemon=True)
    thread.start()
