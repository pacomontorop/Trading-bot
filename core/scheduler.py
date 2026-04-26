"""Minimal long-only equity scheduler loop."""

from __future__ import annotations

import csv
import gc
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from broker.alpaca_live import is_live_enabled
from core.executor import place_long_order
from core.live_executor import place_live_order, tick_protect_live_positions
from core.live_risk_manager import compute_live_plan, get_live_snapshot, load_live_state, record_live_trade
from core.position_protector import tick_protect_positions, close_positions_with_ah_earnings
from core import risk_manager
from core.market_gate import is_us_equity_market_open, get_vix_level
from signals.filters import is_position_open
from signals.reader import get_top_signals
from utils.generate_symbols_csv import generate_symbols_csv
from utils.logger import log_event
from utils.telegram_alert import send_telegram_alert


SYMBOLS_PATH = os.path.join("data", "symbols.csv")
_NY_TZ = ZoneInfo("America/New_York")

# ── Coordination helpers ──────────────────────────────────────────────────────

def _is_cowork_reserved(symbol: str) -> bool:
    """Return True if symbol has a recent Cowork-managed order (prefix COWORK-).

    Cowork tags all its entries with client_order_id='COWORK-YYYYMMDD-SYMBOL'.
    The bot must never open a new position in a Cowork-reserved ticker; it
    would create duplicate exposure and interfere with Cowork's stop/TP logic.
    Checks the last 200 orders (filled + open) for the prefix.
    """
    try:
        from broker import alpaca as broker
        recent = broker.api.list_orders(status="all", limit=200)
        prefix = f"COWORK-"
        sym_upper = symbol.upper()
        for o in (recent or []):
            cid = str(getattr(o, "client_order_id", "") or "")
            if cid.upper().startswith(prefix) and sym_upper in cid.upper():
                # Only block if the order is open/filled (not cancelled)
                status = str(getattr(o, "status", "")).lower()
                if status in ("new", "accepted", "pending_new", "filled",
                              "partially_filled", "held"):
                    return True
    except Exception:
        pass
    return False


def _has_earnings_within(symbol: str, days: int) -> bool:
    """Return True if the symbol has earnings scheduled within `days` calendar days.

    Uses yfinance calendar. Falls back to False on any error so we never
    silently block a valid trade due to a data fetch failure.
    Lesson: MEDP 2026-04-23 — bot entered with AH earnings that day → gap -18%.
    """
    try:
        import yfinance as yf
        from datetime import date, timedelta
        t = yf.Ticker(symbol)
        cal = t.calendar
        if cal is None:
            return False
        # calendar may be a dict or DataFrame depending on yfinance version
        if hasattr(cal, "to_dict"):
            cal = cal.to_dict()
        earn_date = None
        for key in ("Earnings Date", "earningsDate", "earnings_date"):
            if key in cal:
                raw = cal[key]
                if hasattr(raw, "__iter__") and not isinstance(raw, str):
                    raw = list(raw)
                    if raw:
                        raw = raw[0]
                if hasattr(raw, "date"):
                    earn_date = raw.date()
                elif isinstance(raw, date):
                    earn_date = raw
                break
        if earn_date is None:
            return False
        today = date.today()
        return today <= earn_date <= today + timedelta(days=days)
    except Exception:
        return False


_SYMBOLS_MAX_AGE_SEC = 86400  # regenerate universe daily to pick up new listings


def _notify_order(
    symbol: str,
    plan: dict,
    total_score: float,
    quiver_score: float,
    account: str = "PAPER",
) -> None:
    """Send a Telegram alert when an order is placed.

    Fast-lane entries get a prominent warning header since they bypass
    some technical filters. All other approved entries get a standard notice.
    Silently no-ops if Telegram is not configured.
    """
    trace = plan.get("decision_trace") or {}
    fast_lane = trace.get("fast_lane_confirm_status") == "confirmed"
    strong_reasons = (trace.get("quiver_signal_summary") or {}).get("strong_reason", [])
    rsi = trace.get("rsi")
    atr_pct = (trace.get("yahoo_metrics") or {}).get("atr_pct")
    price = plan.get("price") or plan.get("notional", 0) / max(plan.get("qty", 1), 1)
    notional = plan.get("notional", 0)

    header = "FAST-LANE ENTRY" if fast_lane else "ORDER PLACED"
    prefix = "!!" if fast_lane else "--"
    lines = [
        f"{prefix} [{account}] {header}: {symbol}",
        f"   Score: {total_score:.2f}  (quiver {quiver_score:.2f})",
        f"   Price: ${price:.2f}  |  Notional: ${notional:.0f}",
    ]
    if fast_lane and strong_reasons:
        lines.append(f"   Fast-lane trigger: {', '.join(strong_reasons)}")
    if rsi is not None:
        lines.append(f"   RSI: {rsi:.1f}")
    if atr_pct is not None:
        lines.append(f"   ATR%: {atr_pct:.1f}%")
    send_telegram_alert("\n".join(lines))


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
        # Memory management: aggressively GC during the first 20 cycles
        # (market-open burst) then every 10 cycles thereafter.
        # Also flush stale yfinance DataFrames from the scoring cache.
        cycles = _session_stats["cycles_run"]
        if cycles <= 20 or cycles % 10 == 0:
            gc.collect()
        try:
            from signals.scoring import clear_expired_cache
            removed = clear_expired_cache()
            if removed:
                log_event(f"CACHE cleared={removed} expired_entries", event="CACHE")
        except Exception:
            pass

        now_ts = time.time()

        # ── AH earnings forced close (MEDP lesson 2026-04-23) ─────────────
        # Runs every protect cycle (60s). The function itself checks whether
        # current ET time is in the 14:30-15:50 window and bails out early
        # if not, so the call is cheap outside that window.
        if now_ts - last_protect_ts >= 60:
            try:
                close_positions_with_ah_earnings(dry_run=config.DRY_RUN)
            except Exception as exc:
                log_event(f"EARNINGS_CLOSE loop_error err={exc}", event="EARNINGS_CLOSE")

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

        # ── Daily P&L circuit breaker ───────────────────────────────────────
        # If the account is already down beyond the configured threshold today,
        # pause new entries — protect the capital that remains for the next day.
        # Existing positions continue to be protected regardless.
        try:
            _drawdown_threshold = -abs(float(
                ((getattr(config, "_policy", {}) or {}).get("market", {}) or {})
                .get("daily_max_drawdown_pct", 0) or 0
            ))
            if _drawdown_threshold < 0:
                from broker import alpaca as _broker_acct
                _acct = _broker_acct.api.get_account()
                _equity = float(_acct.equity)
                _last_equity = float(_acct.last_equity)
                if _last_equity > 0:
                    _daily_pnl_pct = (_equity - _last_equity) / _last_equity * 100
                    if _daily_pnl_pct <= _drawdown_threshold:
                        _session_stats.setdefault("skips_drawdown", 0)
                        _session_stats["skips_drawdown"] += 1
                        log_event(
                            f"SCAN skipped reason=daily_drawdown_limit "
                            f"pnl_pct={_daily_pnl_pct:.2f} threshold={_drawdown_threshold:.2f}",
                            event="SCAN",
                        )
                        time.sleep(interval_sec)
                        continue
        except Exception as _exc:
            log_event(f"DRAWDOWN_CHECK failed err={_exc}", event="SCAN")

        opportunities, live_extra = get_top_signals(max_symbols=max_symbols)

        # Track universe size after first _cycle_batch() call (independent of results)
        if not _session_stats["symbols_scanned_max"]:
            try:
                from signals.reader import _rot_universe
                if _rot_universe:
                    _session_stats["symbols_scanned_max"] = len(_rot_universe)
            except Exception:
                pass

        live_active = is_live_enabled()

        if not opportunities and not (live_active and live_extra):
            _session_stats["no_signals_cycles"] += 1
            log_event("SCAN end: no approved signals", event="SCAN")
            time.sleep(interval_sec)
            continue

        _session_stats["signals_approved_total"] += len(opportunities)

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
            # ── Coordination gates (earnings + Cowork) ─────────────────────
            # Gate 1: skip symbols reserved by Cowork (client_order_id COWORK-*).
            # Cowork identifies its orders with prefix "COWORK-YYYYMMDD-SYMBOL".
            # If any filled/pending order for this symbol has that prefix, the
            # symbol belongs to Cowork's lifecycle — the bot must not interfere.
            if _is_cowork_reserved(symbol):
                log_event(
                    f"APPROVAL {symbol}: rejected reason=cowork_reserved",
                    event="APPROVAL",
                )
                continue

            # Gate 2: skip symbols with AH earnings today or within N days.
            # policy.yaml: earnings.avoid_entry_if_earnings_within_days
            _earn_cfg = config.cfg.get("earnings", {})
            _avoid_days = int(_earn_cfg.get("avoid_entry_if_earnings_within_days", 1))
            if _avoid_days > 0 and _has_earnings_within(symbol, _avoid_days):
                log_event(
                    f"APPROVAL {symbol}: rejected reason=earnings_within_{_avoid_days}d",
                    event="APPROVAL",
                )
                continue

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
                    _notify_order(symbol, plan, total_score, quiver_score, account="PAPER")
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
                    _notify_order(symbol, plan, total_score, quiver_score, account="LIVE")
                else:
                    log_event(f"LIVE ORDER {symbol}: failed", event="LIVE")
            except Exception as exc:
                log_event(f"LIVE ORDER {symbol}: error err={exc}", event="LIVE")

        # ── Live-only signals (paper full, live has capacity) ───────────────
        # Signals rejected by paper solely due to max_exposure are evaluated
        # here for the live account only — paper is intentionally skipped.
        # Cap to 2 per cycle so live never fires a burst of orders in one tick;
        # daily_max_cash_pct in live_account policy provides the session-level cap.
        _live_extra_limit = 2
        if live_active and live_extra:
            for symbol, total_score, quiver_score, price, atr, plan in live_extra[:_live_extra_limit]:
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
                            f"LIVE ORDER {symbol}: rejected reason={reason} (paper_exposure_bypass)",
                            event="LIVE",
                        )
                        continue
                    log_event(
                        (
                            f"LIVE ORDER {symbol}: attempt score={total_score:.2f} "
                            f"qty={live_plan['qty']} notional={live_plan['notional']:.2f} "
                            f"(paper_exposure_bypass)"
                        ),
                        event="LIVE",
                    )
                    live_success = place_live_order(live_plan, dry_run=config.DRY_RUN)
                    if live_success:
                        _session_stats["live_orders_placed"] += 1
                        log_event(f"LIVE ORDER {symbol}: submitted (paper_exposure_bypass)", event="LIVE")
                        if not config.DRY_RUN:
                            record_live_trade(live_plan)
                        _notify_order(symbol, plan, total_score, quiver_score, account="LIVE")
                    else:
                        log_event(f"LIVE ORDER {symbol}: failed (paper_exposure_bypass)", event="LIVE")
                except Exception as exc:
                    log_event(f"LIVE ORDER {symbol}: error err={exc} (paper_exposure_bypass)", event="LIVE")

        time.sleep(interval_sec)

