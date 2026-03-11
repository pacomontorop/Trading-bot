"""Minimal long-only equity scheduler loop."""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from broker.alpaca_live import is_live_enabled
from core.executor import place_long_order
from core.live_executor import place_live_order, tick_protect_live_positions
from core.live_risk_manager import compute_live_plan, get_live_snapshot, load_live_state, record_live_trade
from core.position_protector import tick_protect_positions
from core import risk_manager
from core.market_gate import is_us_equity_market_open, get_vix_level
from signals.filters import is_position_open
from signals.reader import get_top_signals
from utils.generate_symbols_csv import generate_symbols_csv
from utils.logger import log_event


SYMBOLS_PATH = os.path.join("data", "symbols.csv")
_NY_TZ = ZoneInfo("America/New_York")


_SYMBOLS_MAX_AGE_SEC = 86400  # regenerate universe daily to pick up new listings


def _symbols_csv_valid(path: str) -> bool:
    if not os.path.exists(path):
        return False
    if time.time() - os.path.getmtime(path) > _SYMBOLS_MAX_AGE_SEC:
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


def equity_scheduler_loop(interval_sec: int = 15, max_symbols: int | None = None) -> None:
    """Run the single equity scheduler loop.

    interval_sec: sleep between cycles. The scan itself takes ~1.5-3 min
    (30 symbols × ~3s Yahoo each). 15s sleep adds minimal overhead while
    avoiding a tight loop if all symbols are in cooldown / cache-hit.
    max_symbols: symbols to evaluate per cycle. None = read from policy.yaml
    (signals.max_symbols_per_scan, default 100).  Policy wins over the old
    hard-coded 100 default so the YAML is the single source of truth.
    """

    _ensure_symbols_csv()
    log_event("Scheduler loop started (equities, long-only)", event="SCAN")

    last_protect_ts = 0.0
    last_live_protect_ts = 0.0

    # ── Session tracking ────────────────────────────────────────────────────
    _prev_market_open: bool = False
    _summary_sent_date: str | None = None
    _session_stats: dict = {}

    def _reset_session_stats(today: str) -> dict:
        return {
            "date": today,
            "cycles_run": 0,
            "symbols_scanned_max": 0,  # filled from reader rotation state
            "signals_approved_total": 0,
            "no_signals_cycles": 0,
            "skips_vix": 0,
            "vix_last": 0.0,
            "orders_placed": 0,
            "orders_position_open": 0,
            "orders_failed": 0,
            "live_orders_placed": 0,
            "live_orders_rejected": 0,
            "live_rejection_counts": {},
        }

    while True:
        _today = datetime.now(tz=_NY_TZ).strftime("%Y-%m-%d")
        if _session_stats.get("date") != _today:
            _session_stats = _reset_session_stats(_today)
            if _summary_sent_date != _today:
                _summary_sent_date = None  # allow fresh send for the new day

        market_open = is_us_equity_market_open()
        log_event(f"Market gate check open={market_open}", event="GATE")
        if not market_open:
            # Detect close transition: market just shut — send summary once.
            # Use a file-based flag to prevent duplicate emails when multiple
            # Render instances are running simultaneously (e.g. during deploys).
            if _prev_market_open and _session_stats.get("cycles_run", 0) > 0 and _summary_sent_date != _today:
                from utils.state import try_claim_summary_send
                try:
                    if try_claim_summary_send(_today):
                        _summary_sent_date = _today
                        from utils.daily_summary import send_session_summary
                        send_session_summary(_session_stats)
                    else:
                        _summary_sent_date = _today  # another instance already sent it
                        log_event("SESSION_SUMMARY skip reason=already_sent_by_another_instance", event="SUMMARY")
                except Exception as _exc:
                    log_event(f"SESSION_SUMMARY trigger failed err={_exc}", event="SUMMARY")
            _prev_market_open = False
            log_event("SCAN skipped reason=market_closed", event="SCAN")
            time.sleep(interval_sec)
            continue

        _prev_market_open = True
        _session_stats["cycles_run"] += 1

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
        if vix_level > 0:
            _session_stats["vix_last"] = vix_level
        if vix_elevated:
            _session_stats["skips_vix"] += 1
            log_event(
                f"SCAN skipped reason=high_vix vix={vix_level:.1f}",
                event="SCAN",
            )
            time.sleep(interval_sec)
            continue

        opportunities = get_top_signals(max_symbols=max_symbols)

        # Track universe size after first _cycle_batch() call (independent of results)
        if not _session_stats["symbols_scanned_max"]:
            try:
                from signals.reader import _rot_universe
                if _rot_universe:
                    _session_stats["symbols_scanned_max"] = len(_rot_universe)
            except Exception:
                pass

        if not opportunities:
            _session_stats["no_signals_cycles"] += 1
            log_event("SCAN end: no approved signals", event="SCAN")
            time.sleep(interval_sec)
            continue

        _session_stats["signals_approved_total"] += len(opportunities)

        live_active = is_live_enabled()

        # ── Pre-fetch live account state once for the whole cycle ───────────
        # Avoids N broker API calls (account + positions + orders) when there
        # are N approved signals.  The snapshot is read-only within the loop;
        # record_live_trade() updates the on-disk state file after each fill.
        live_snapshot = None
        live_state = None
        if live_active:
            try:
                live_snapshot = get_live_snapshot()
                live_state = load_live_state()
                log_event(
                    f"LIVE trading active for this cycle "
                    f"cash={live_snapshot['cash']:.0f} "
                    f"open_positions={len(live_snapshot['positions'])}",
                    event="LIVE",
                )
            except Exception as exc:
                log_event(f"LIVE snapshot fetch failed err={exc} — skipping live this cycle", event="LIVE")
                live_active = False

        for symbol, total_score, quiver_score, price, atr, plan in opportunities:
            # ── Paper account trade ─────────────────────────────────────────
            if is_position_open(symbol):
                _session_stats["orders_position_open"] += 1
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
                    _session_stats["orders_placed"] += 1
                    log_event(f"ORDER {symbol}: submitted", event="ORDER")
                    if not config.DRY_RUN:
                        risk_manager.record_trade(plan)
                else:
                    _session_stats["orders_failed"] += 1
                    log_event(f"ORDER {symbol}: failed", event="ORDER")

            # ── Live account trade (same signal, independent sizing) ────────
            # Snapshot and state are reused from the pre-fetch above —
            # no extra broker API calls per symbol.
            if not live_active:
                continue
            try:
                live_plan, reason = compute_live_plan(
                    symbol=symbol,
                    price=price or 0.0,
                    atr=atr,
                    snapshot=live_snapshot,
                    state=live_state,
                )
                if live_plan is None:
                    _session_stats["live_orders_rejected"] += 1
                    _rej = reason or "unknown"
                    _session_stats["live_rejection_counts"][_rej] = (
                        _session_stats["live_rejection_counts"].get(_rej, 0) + 1
                    )
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
                    _session_stats["live_orders_placed"] += 1
                    log_event(f"LIVE ORDER {symbol}: submitted", event="LIVE")
                    if not config.DRY_RUN:
                        record_live_trade(live_plan)
                else:
                    log_event(f"LIVE ORDER {symbol}: failed", event="LIVE")
            except Exception as exc:
                log_event(f"LIVE ORDER {symbol}: error err={exc}", event="LIVE")

        time.sleep(interval_sec)
