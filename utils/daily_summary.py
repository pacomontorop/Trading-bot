"""End-of-session email summary for the trading bot.

Called automatically from the scheduler when the market transitions
from open → closed.  Only sent on days when at least one trading cycle ran.

JSON state structure (verified against risk_manager.py / live_risk_manager.py):

  risk_state.json (DailyRiskState.to_dict):
    date, spent_today_usd, new_positions_today, symbols_traded_today,
    symbol_last_trade, last_trade_time, blocked_reason

  risk_state_live.json (LiveDailyState.to_dict):
    date, spent_today_usd, new_positions_today, symbols_traded_today,
    symbol_last_trade
    (NO last_trade_time, NO blocked_reason)
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

# Paths relative to project root (CWD when scheduler runs)
_PAPER_STATE = os.path.join("data", "risk_state.json")
_LIVE_STATE = os.path.join("data", "risk_state_live.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: str) -> dict:
    """Load a JSON file; return {} on any error."""
    try:
        abs_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), path)
        with open(abs_path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _policy(*keys: str, default: Any = None) -> Any:
    """Safe nested lookup in config._policy. Returns default when missing."""
    node: Any = getattr(config, "_policy", None) or {}
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k)
        if node is None:
            return default
    return node


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _time_et(iso: str) -> str | None:
    """Convert ISO timestamp to 'HH:MM ET'. Returns None on failure."""
    try:
        return datetime.fromisoformat(iso).astimezone(NY_TZ).strftime("%H:%M ET")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def build_session_summary(session_stats: dict) -> str:
    """Return a plain-text email body.

    Rules:
    - Skip any field whose value is None, 0, False, or an empty list.
    - 'No trades' sections explain the cause rather than printing zeros.
    """
    now_ny = datetime.now(tz=NY_TZ)
    today = now_ny.strftime("%Y-%m-%d")

    lines: list[str] = []
    W = 26  # label column width

    def row(label: str, value: str) -> None:
        lines.append(f"  {label:<{W}} {value}")

    lines.append(f"Trading Session Summary — {today}")
    lines.append(f"Sent at close: {now_ny.strftime('%H:%M ET')}")
    lines.append("=" * 50)

    # ── Session activity ────────────────────────────────────────────────────
    cycles: int = session_stats.get("cycles_run", 0)
    scanned: int = session_stats.get("symbols_scanned_max", 0)
    approved_total: int = session_stats.get("signals_approved_total", 0)
    no_sig_cycles: int = session_stats.get("no_signals_cycles", 0)
    skips_vix: int = session_stats.get("skips_vix", 0)
    vix_last: float = float(session_stats.get("vix_last") or 0)
    vix_thr: float = float(_policy("market", "vix_pause_threshold") or 0)

    lines.append("")
    if cycles:
        row("Cycles run:", str(cycles))
    if scanned:
        row("Universe:", f"{scanned} symbols")
    if approved_total:
        row("Gate-approved signals:", str(approved_total))
    if no_sig_cycles and cycles:
        row("Cycles without signals:", f"{no_sig_cycles}/{cycles}")
    if skips_vix:
        row("Cycles paused (VIX):", f"{skips_vix}  [VIX={vix_last:.1f}, limit={vix_thr:.0f}]")
    elif vix_last > 0:
        row("VIX (last):", f"{vix_last:.1f}  (limit={vix_thr:.0f} — OK)")

    # ── Paper account ───────────────────────────────────────────────────────
    lines.append("")
    lines.append("--- Paper Account ---")
    paper = _load_json(_PAPER_STATE)
    paper_today = paper.get("date") == today

    orders_placed: int = session_stats.get("orders_placed", 0)
    orders_pos_open: int = session_stats.get("orders_position_open", 0)
    orders_failed: int = session_stats.get("orders_failed", 0)

    # The state file is only written when a trade executes (record_trade).
    # On no-trade days the file is absent or carries yesterday's date — normal.
    if paper_today:
        positions: int = int(paper.get("new_positions_today") or 0)
        spent: float = float(paper.get("spent_today_usd") or 0)
        symbols: list[str] = [s for s in (paper.get("symbols_traded_today") or []) if s]
        last_trade: str | None = paper.get("last_trade_time")
        blocked: str | None = paper.get("blocked_reason")

        if positions > 0:
            row("Positions opened:", str(positions))
        if spent > 0:
            row("Notional spent:", _usd(spent))
        if symbols:
            row("Symbols traded:", ", ".join(symbols))
        if last_trade:
            t = _time_et(last_trade)
            if t:
                row("Last order at:", t)
        if blocked:
            row("Blocked reason:", blocked)

    if orders_placed == 0:
        # Explain why no paper trades (applies whether state file exists or not)
        causes: list[str] = []
        if approved_total == 0:
            causes.append("no signals passed all gates")
        if orders_pos_open:
            causes.append(f"position already open ({orders_pos_open}× skipped)")
        if orders_failed:
            causes.append(f"{orders_failed} order placement(s) failed")
        if skips_vix:
            causes.append(f"VIX too high ({skips_vix} cycles paused)")
        if _policy("market", "global_kill_switch"):
            causes.append("global kill switch is ON")
        if not _policy("safeguards", "enabled", default=True):
            causes.append("safeguards inactive — orders blocked")
        if causes:
            row("No trades:", "; ".join(causes))
        else:
            lines.append("  No trades executed today.")
    elif paper_today:
        if orders_pos_open:
            row("Skipped (pos. open):", f"{orders_pos_open}")
        if orders_failed:
            row("Order errors:", f"{orders_failed}")

    # ── Live account (only when enabled) ────────────────────────────────────
    live_enabled = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    if live_enabled:
        lines.append("")
        lines.append("--- Live Account ---")
        live = _load_json(_LIVE_STATE)
        live_today = live.get("date") == today

        live_placed: int = session_stats.get("live_orders_placed", 0)
        live_rejected: int = session_stats.get("live_orders_rejected", 0)
        live_rej: dict[str, int] = session_stats.get("live_rejection_counts", {})

        # State file only written on trade days — absent file = no trades (normal).
        if live_today:
            l_pos: int = int(live.get("new_positions_today") or 0)
            l_spent: float = float(live.get("spent_today_usd") or 0)
            l_syms: list[str] = [s for s in (live.get("symbols_traded_today") or []) if s]

            if l_pos > 0:
                row("Positions opened:", str(l_pos))
            if l_spent > 0:
                row("Notional spent:", _usd(l_spent))
            if l_syms:
                row("Symbols traded:", ", ".join(l_syms))

        if live_placed == 0:
            if live_rej:
                top = sorted(live_rej.items(), key=lambda x: -x[1])[:5]
                row("No live trades —", "top rejections:")
                for reason, cnt in top:
                    lines.append(f"    {reason}: {cnt}×")
            elif live_rejected:
                row("No live trades:", f"{live_rejected} signals rejected")
            else:
                lines.append("  No live trades executed today.")
        elif live_today:
            if live_rejected:
                row("Signals rejected:", str(live_rejected))

    # ── Key policy parameters ────────────────────────────────────────────────
    lines.append("")
    lines.append("--- Key Parameters ---")

    def _fmt_param(val: Any, fmt: str) -> str:
        """Format a policy parameter; return 'no limit' for falsy numeric values."""
        if val is None:
            return ""
        if fmt in ("usd", "int", "float") and float(val) == 0:
            return "no limit"
        if fmt == "usd":
            return _usd(float(val))
        if fmt == "float":
            return f"{float(val):.2f}"
        return str(val)

    # daily_max_spend_usd is 0 when pct-of-buying-power is the real limiter
    spend_usd = _policy("risk", "daily_max_spend_usd")
    spend_pct = _policy("risk", "daily_max_spend_pct_buying_power")
    if spend_usd:
        row("Daily max spend:", _usd(float(spend_usd)))
    elif spend_pct:
        row("Daily max spend:", f"{float(spend_pct)*100:.0f}% buying power")

    params: list[tuple[str, Any, str]] = [
        ("Max positions/day:",    _policy("risk", "daily_max_new_positions"),   "int"),
        ("Max open positions:",   _policy("risk", "max_total_open_positions"),  "int"),
        ("Max position size:",    _policy("risk", "max_position_size_usd"),     "usd"),
        ("Score threshold:",      _policy("signals", "approval_threshold"),     "float"),
        ("ATR stop mult:",        _policy("risk", "atr_k"),                     "float"),
        ("Take-profit ATR mult:", _policy("execution", "take_profit_atr_mult"), "float"),
        ("Min R:R ratio:",        _policy("execution", "min_rr_ratio"),         "float"),
    ]
    for label, val, fmt in params:
        if val is None:
            continue
        formatted = _fmt_param(val, fmt)
        if formatted:
            row(label, formatted)

    kill = _policy("market", "global_kill_switch")
    if kill:
        lines.append("  !! GLOBAL KILL SWITCH: ON — no new orders !!")
    safeguards_ok = _policy("safeguards", "enabled", default=True)
    if not safeguards_ok:
        lines.append("  !! SAFEGUARDS INACTIVE — orders blocked !!")

    lines.append("")
    lines.append("=" * 50)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def send_session_summary(session_stats: dict) -> None:
    """Build and deliver the end-of-session email. Swallows all exceptions."""
    try:
        body = build_session_summary(session_stats)
        date_str = datetime.now(tz=NY_TZ).strftime("%Y-%m-%d")
        subject = f"[Trading Bot] Session Summary {date_str}"
        send_email(subject, body)
        log_event("SESSION_SUMMARY email sent successfully", event="SUMMARY")
    except Exception as exc:
        log_event(f"SESSION_SUMMARY email failed err={exc}", event="SUMMARY")
