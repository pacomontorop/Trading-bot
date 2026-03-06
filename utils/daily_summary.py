"""End-of-session email summary for the trading bot.

Called automatically from the scheduler when the market transitions
from open → closed.  Only sent on days when at least one trading cycle ran.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import config
from utils.emailer import send_email
from utils.logger import log_event

NY_TZ = ZoneInfo("America/New_York")
_RISK_STATE_PATH = "data/risk_state.json"
_LIVE_STATE_PATH = "data/risk_state_live.json"


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _policy_val(*keys: str, default: Any = None) -> Any:
    node = getattr(config, "_policy", {}) or {}
    for k in keys:
        node = (node or {}).get(k)
        if node is None:
            return default
    return node


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(NY_TZ).strftime("%H:%M ET")
    except Exception:
        return iso


def build_session_summary(session_stats: dict) -> str:
    """Return a plain-text email body.  Omits any null/zero/empty field."""
    now_ny = datetime.now(tz=NY_TZ)
    date_str = now_ny.strftime("%Y-%m-%d")
    time_str = now_ny.strftime("%H:%M ET")

    lines: list[str] = []
    lines.append(f"Trading Session Summary — {date_str}")
    lines.append(f"Sent at close: {time_str}")
    lines.append("=" * 48)

    # ── Session activity ────────────────────────────────────────────────────
    cycles = session_stats.get("cycles_run", 0)
    if cycles:
        lines.append(f"\nTrading cycles run:       {cycles}")

    scanned = session_stats.get("symbols_scanned_max", 0)
    if scanned:
        lines.append(f"Universe size:            {scanned} symbols")

    signals_total = session_stats.get("signals_approved_total", 0)
    if signals_total:
        lines.append(f"Gate-approved signals:    {signals_total}")

    no_sig = session_stats.get("no_signals_cycles", 0)
    if no_sig and cycles:
        lines.append(f"Cycles with no signals:   {no_sig}/{cycles}")

    skips_vix = session_stats.get("skips_vix", 0)
    vix_last = session_stats.get("vix_last", 0.0)
    vix_thr = float(_policy_val("market", "vix_pause_threshold", default=0) or 0)
    if skips_vix:
        lines.append(
            f"Cycles paused (VIX):      {skips_vix}  "
            f"[VIX={vix_last:.1f}, threshold={vix_thr:.0f}]"
        )
    elif vix_last > 0:
        lines.append(f"VIX (last seen):          {vix_last:.1f}  (threshold={vix_thr:.0f}, OK)")

    # ── Paper account ───────────────────────────────────────────────────────
    lines.append("\n--- Paper Account ---")
    paper = _load_json(_RISK_STATE_PATH)
    if paper and paper.get("date") == date_str:
        positions = int(paper.get("new_positions_today", 0))
        spent = float(paper.get("spent_today_usd", 0))
        symbols = [s for s in paper.get("symbols_traded_today", []) if s]
        last_trade = paper.get("last_trade_time")

        if positions > 0:
            lines.append(f"Positions opened:         {positions}")
        if spent > 0:
            lines.append(f"Notional spent:           {_fmt_usd(spent)}")
        if symbols:
            lines.append(f"Symbols traded:           {', '.join(symbols)}")
        if last_trade:
            lines.append(f"Last trade at:            {_fmt_time(last_trade)}")

        orders_placed = session_stats.get("orders_placed", 0)
        orders_pos_open = session_stats.get("orders_position_open", 0)
        orders_failed = session_stats.get("orders_failed", 0)

        if orders_placed == 0:
            reasons: list[str] = []
            if signals_total == 0:
                reasons.append("no qualifying signals from scanner")
            if orders_pos_open:
                reasons.append(f"position already open ({orders_pos_open} skipped)")
            if orders_failed:
                reasons.append(f"{orders_failed} order placement(s) failed")
            if skips_vix:
                reasons.append(f"VIX elevated ({skips_vix} cycles paused)")
            kill = _policy_val("market", "global_kill_switch", default=False)
            if kill:
                reasons.append("global kill switch is ON")
            if reasons:
                lines.append(f"No trades because:        {'; '.join(reasons)}")
            else:
                lines.append("No trades executed today.")
        else:
            if orders_pos_open:
                lines.append(f"Skipped (position open):  {orders_pos_open} signals")
            if orders_failed:
                lines.append(f"Order placement errors:   {orders_failed}")
    else:
        lines.append("No trading state available for today.")

    # ── Live account ────────────────────────────────────────────────────────
    live_enabled = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    if live_enabled:
        lines.append("\n--- Live Account ---")
        live = _load_json(_LIVE_STATE_PATH)
        if live and live.get("date") == date_str:
            l_positions = int(live.get("new_positions_today", 0))
            l_spent = float(live.get("spent_today_usd", 0))
            l_symbols = [s for s in live.get("symbols_traded_today", []) if s]
            l_last = live.get("last_trade_time")

            if l_positions > 0:
                lines.append(f"Positions opened:         {l_positions}")
            if l_spent > 0:
                lines.append(f"Notional spent:           {_fmt_usd(l_spent)}")
            if l_symbols:
                lines.append(f"Symbols traded:           {', '.join(l_symbols)}")
            if l_last:
                lines.append(f"Last trade at:            {_fmt_time(l_last)}")

            live_placed = session_stats.get("live_orders_placed", 0)
            live_rejected = session_stats.get("live_orders_rejected", 0)
            live_rej_counts: dict[str, int] = session_stats.get("live_rejection_counts", {})

            if live_placed == 0:
                if live_rej_counts:
                    top = sorted(live_rej_counts.items(), key=lambda x: -x[1])[:5]
                    reasons_str = "; ".join(f"{r}×{c}" for r, c in top)
                    lines.append(f"No live trades — rejections: {reasons_str}")
                elif live_rejected:
                    lines.append(f"No live trades ({live_rejected} rejected).")
                else:
                    lines.append("No live trades executed today.")
            elif live_rejected:
                lines.append(f"Signals rejected (live):  {live_rejected}")
        else:
            lines.append("No live trading state available for today.")

    # ── Key policy parameters ────────────────────────────────────────────────
    lines.append("\n--- Key Parameters ---")
    params: list[tuple[str, Any, str]] = [
        ("Daily max spend",       _policy_val("risk", "daily_max_spend_usd"),         "usd"),
        ("Max new positions/day", _policy_val("risk", "daily_max_new_positions"),      "int"),
        ("Max open positions",    _policy_val("risk", "max_total_open_positions"),     "int"),
        ("Max position size",     _policy_val("risk", "max_position_size_usd"),        "usd"),
        ("Score threshold",       _policy_val("signals", "approval_threshold"),        "float"),
        ("ATR stop multiplier",   _policy_val("risk", "atr_k"),                        "float"),
        ("Take-profit ATR mult",  _policy_val("execution", "take_profit_atr_mult"),    "float"),
        ("Min R:R ratio",         _policy_val("execution", "min_rr_ratio"),            "float"),
    ]
    for label, val, fmt in params:
        if val is None:
            continue
        if fmt == "usd":
            lines.append(f"  {label:<24} {_fmt_usd(float(val))}")
        elif fmt == "float":
            lines.append(f"  {label:<24} {float(val):.2f}")
        else:
            lines.append(f"  {label:<24} {val}")

    kill = _policy_val("market", "global_kill_switch", default=False)
    if kill:
        lines.append("  GLOBAL KILL SWITCH:      ON — no new orders")
    safeguards = _policy_val("safeguards", "enabled", default=True)
    if not safeguards:
        lines.append("  SAFEGUARDS:              INACTIVE — orders blocked")

    lines.append("\n" + "=" * 48)
    return "\n".join(lines)


def send_session_summary(session_stats: dict) -> None:
    """Build and deliver the end-of-session email summary."""
    try:
        body = build_session_summary(session_stats)
        date_str = datetime.now(tz=NY_TZ).strftime("%Y-%m-%d")
        subject = f"[Trading Bot] Session Summary {date_str}"
        send_email(subject, body)
        log_event("SESSION_SUMMARY email sent successfully", event="SUMMARY")
    except Exception as exc:
        log_event(f"SESSION_SUMMARY email failed err={exc}", event="SUMMARY")
