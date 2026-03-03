"""Minimal long-only equity scheduler loop."""

from __future__ import annotations

import csv
import os
import time

import config
from broker.alpaca_live import is_live_enabled
from core.executor import place_long_order
from core.live_executor import place_live_order, tick_protect_live_positions
from core.live_risk_manager import compute_live_plan, record_live_trade
from core.position_protector import tick_protect_positions
from core import risk_manager
from core.market_gate import is_us_equity_market_open, get_vix_level
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

    last_protect_ts = 0.0
    last_live_protect_ts = 0.0

    while True:
        market_open = is_us_equity_market_open()
        log_event(f"Market gate check open={market_open}", event="GATE")
        if not market_open:
            log_event("SCAN skipped reason=market_closed", event="SCAN")
            time.sleep(interval_sec)
            continue

        now_ts = time.time()

        # ── Paper account position protection ──────────────────────────────
        if now_ts - last_protect_ts >= 60:
            try:
                tick_protect_positions(dry_run=config.DRY_RUN)
            except Exception as exc:
                log_event(f"PROTECT loop_error err={exc}", event="PROTECT")
            finally:
                last_protect_ts = now_ts

        # ── Live account position protection ───────────────────────────────
        # Runs independently of live trading being enabled for new entries —
        # once live positions exist they must always be protected.
        if now_ts - last_live_protect_ts >= 60:
            try:
                tick_protect_live_positions(dry_run=config.DRY_RUN)
            except Exception as exc:
                log_event(f"LIVE_PROTECT loop_error err={exc}", event="LIVE")
            finally:
                last_live_protect_ts = now_ts

        # VIX fear gate — pause new entries when market fear is elevated.
        # Existing positions are always protected above regardless.
        vix_level, vix_elevated = get_vix_level()
        if vix_elevated:
            log_event(
                f"SCAN skipped reason=high_vix vix={vix_level:.1f}",
                event="SCAN",
            )
            time.sleep(interval_sec)
            continue

        opportunities = get_top_signals(max_symbols=max_symbols)
        if not opportunities:
            log_event("SCAN end: no approved signals", event="SCAN")
            time.sleep(interval_sec)
            continue

        live_active = is_live_enabled()
        if live_active:
            log_event("LIVE trading active for this scan cycle", event="LIVE")

        for symbol, total_score, quiver_score, price, atr, plan in opportunities:
            # ── Paper account trade ─────────────────────────────────────────
            if is_position_open(symbol):
                log_event(
                    f"APPROVAL {symbol}: rejected reason=position_open",
                    event="APPROVAL",
                )
            else:
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

            # ── Live account trade (same signal, independent sizing) ────────
            if not live_active:
                continue
            try:
                live_plan, reason = compute_live_plan(
                    symbol=symbol, price=price or 0.0, atr=atr
                )
                if live_plan is None:
                    log_event(
                        f"LIVE ORDER {symbol}: rejected reason={reason}",
                        event="LIVE",
                    )
                    continue
                log_event(
                    (
                        f"LIVE ORDER {symbol}: attempt score={total_score:.2f} "
                        f"qty={live_plan['qty']} notional={live_plan['notional']:.2f}"
                    ),
                    event="LIVE",
                )
                live_success = place_live_order(live_plan, dry_run=config.DRY_RUN)
                if live_success:
                    log_event(f"LIVE ORDER {symbol}: submitted", event="LIVE")
                    if not config.DRY_RUN:
                        record_live_trade(live_plan)
                else:
                    log_event(f"LIVE ORDER {symbol}: failed", event="LIVE")
            except Exception as exc:
                log_event(f"LIVE ORDER {symbol}: error err={exc}", event="LIVE")

        time.sleep(interval_sec)
