"""Risk management helpers for the live (real-money) Alpaca account.

Position sizing is capped at a fraction of available live cash
(``live_account.max_cash_pct`` in ``config/policy.yaml``, default 20 %).

The live account maintains its own daily state in ``data/risk_state_live.json``,
completely separate from the paper account state (``data/risk_state.json``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import config
from core.order_protection import compute_bracket_prices, validate_bracket_prices
from core.safeguards import is_safeguards_active
from utils.logger import log_event

NY_TZ = ZoneInfo("America/New_York")
LIVE_STATE_PATH = "data/risk_state_live.json"


def _live_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("live_account", {}) or {}


def _risk_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("risk", {}) or {}


def _exec_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("execution", {}) or {}


def _today_nyse() -> str:
    return datetime.now(tz=NY_TZ).strftime("%Y-%m-%d")


@dataclass
class LiveDailyState:
    date: str
    spent_today_usd: float = 0.0
    new_positions_today: int = 0
    symbols_traded_today: list[str] = field(default_factory=list)
    symbol_last_trade: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "spent_today_usd": self.spent_today_usd,
            "new_positions_today": self.new_positions_today,
            "symbols_traded_today": self.symbols_traded_today,
            "symbol_last_trade": self.symbol_last_trade,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], date: str) -> "LiveDailyState":
        return cls(
            date=date,
            spent_today_usd=float(data.get("spent_today_usd", 0.0)),
            new_positions_today=int(data.get("new_positions_today", 0)),
            symbols_traded_today=list(data.get("symbols_traded_today", [])),
            symbol_last_trade=dict(data.get("symbol_last_trade", {})),
        )


def load_live_state() -> LiveDailyState:
    today = _today_nyse()
    if not os.path.exists(LIVE_STATE_PATH):
        return LiveDailyState(date=today)
    try:
        with open(LIVE_STATE_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle) or {}
    except Exception:
        return LiveDailyState(date=today)
    stored_date = payload.get("date")
    state = LiveDailyState.from_dict(payload, date=today)
    if stored_date != today:
        state.spent_today_usd = 0.0
        state.new_positions_today = 0
        state.symbols_traded_today = []
    return state


def save_live_state(state: LiveDailyState) -> None:
    try:
        os.makedirs(os.path.dirname(LIVE_STATE_PATH), exist_ok=True)
        with open(LIVE_STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle)
    except Exception as exc:
        log_event(f"LIVE_RISK state save failed err={exc}", event="LIVE")


def get_live_snapshot() -> dict | None:
    """Fetch live account equity, cash, positions and orders in one call.

    Call this **once per scheduler cycle** and pass the result to
    :func:`compute_live_plan` to avoid redundant broker API calls.
    """
    return _get_live_snapshot()


def _get_live_snapshot() -> dict | None:
    from broker.alpaca_live import live_api, list_live_positions, list_live_open_orders

    try:  # pragma: no cover - network
        account = live_api.get_account()
        equity = float(getattr(account, "equity", 0) or 0)
        cash = float(getattr(account, "cash", 0) or 0)
        positions = list_live_positions()
        orders = list_live_open_orders()
    except Exception as exc:  # pragma: no cover
        log_event(f"LIVE_RISK account fetch failed err={exc}", event="LIVE")
        return None

    total_exposure = 0.0
    symbol_exposure: dict[str, float] = {}
    for pos in positions:
        try:
            market_value = float(getattr(pos, "market_value", 0) or 0)
            symbol = getattr(pos, "symbol", "")
            total_exposure += max(market_value, 0.0)
            symbol_exposure[symbol] = max(market_value, 0.0)
        except Exception:
            continue

    return {
        "equity": equity,
        "cash": cash,
        "positions": positions,
        "orders": orders,
        "total_exposure": total_exposure,
        "symbol_exposure": symbol_exposure,
    }


def compute_live_plan(
    *,
    symbol: str,
    price: float,
    atr: float | None,
    snapshot: dict | None = None,
    state: "LiveDailyState | None" = None,
) -> tuple[dict | None, str]:
    """Compute a conservative order plan sized for the live account.

    Position notional is capped at:
    - ``max_cash_pct * available_cash`` (default 20 % of cash), AND
    - ``max_position_size_usd`` (hard cap, default $200)

    ``snapshot`` and ``state`` can be provided by the caller to avoid
    redundant network/disk I/O when evaluating multiple symbols in one cycle.

    Returns ``(plan, reason)``; ``plan`` is ``None`` when the trade is
    rejected and ``reason`` explains why.
    """
    if not is_safeguards_active():
        return None, "safeguards_inactive"

    live_cfg = _live_cfg()
    risk_cfg = _risk_cfg()
    exec_cfg = _exec_cfg()

    max_cash_pct = float(live_cfg.get("max_cash_pct", 0.20))
    max_pos_usd = float(live_cfg.get("max_position_size_usd", 200))
    min_pos_usd = float(live_cfg.get("min_position_size_usd", 50))
    daily_max_new = int(live_cfg.get("daily_max_new_positions", 2))
    max_total_open = int(live_cfg.get("max_total_open_positions", 5))
    cooldown_days = int(live_cfg.get("symbol_cooldown_days", 5))

    if price <= 0:
        return None, "invalid_price"

    if state is None:
        state = load_live_state()
    if snapshot is None:
        snapshot = _get_live_snapshot()
    if snapshot is None:
        return None, "live_account_unavailable"

    cash = snapshot["cash"]
    equity = snapshot["equity"]
    positions = snapshot["positions"]
    orders = snapshot["orders"]

    if equity <= 0:
        return None, "invalid_equity"

    # Daily position count limit
    if daily_max_new and state.new_positions_today >= daily_max_new:
        return None, "live_daily_positions_exceeded"

    # Total open position limit
    if max_total_open and len(positions) >= max_total_open:
        return None, "live_max_open_positions"

    # Already have a position in this symbol
    if any(getattr(pos, "symbol", "") == symbol for pos in positions):
        return None, "live_position_open"

    # Already have an open order for this symbol
    if any(getattr(o, "symbol", "") == symbol for o in (orders or [])):
        return None, "live_order_pending"

    # Already traded this symbol today
    if symbol in state.symbols_traded_today:
        return None, "live_symbol_traded_today"

    # Symbol cooldown check
    if cooldown_days:
        last_date = state.symbol_last_trade.get(symbol)
        if last_date:
            try:
                last_dt = datetime.strptime(last_date, "%Y-%m-%d")
                delta = (datetime.now(tz=NY_TZ) - last_dt.replace(tzinfo=NY_TZ)).days
                if delta < cooldown_days:
                    return None, "live_symbol_cooldown"
            except Exception:
                pass

    # Budget: cash_pct cap, hard cap, and remaining session budget
    cash_budget = cash * max_cash_pct
    already_committed = state.spent_today_usd
    remaining_session = (max_pos_usd * daily_max_new) - already_committed
    budget = min(cash_budget, max_pos_usd, max(remaining_session, 0))

    if budget < min_pos_usd:
        return None, "live_budget_too_small"

    atr_val = float(atr or 0.0)
    atr_k = float(risk_cfg.get("atr_k", 2.0))
    min_stop_pct = float(risk_cfg.get("min_stop_pct", 0.05))
    risk_pct = float(risk_cfg.get("max_symbol_risk_pct", 0.01))

    stop_distance = max(atr_val * atr_k, price * min_stop_pct)
    if stop_distance <= 0:
        return None, "invalid_stop_distance"

    # Risk-based qty: risk at most 1% of live equity on stop distance
    risk_qty = int((equity * risk_pct) / stop_distance)
    # Budget-based qty: stay within cash budget
    budget_qty = int(budget / price)
    qty = min(risk_qty, budget_qty)

    if qty < 1:
        return None, "live_qty_below_one"

    notional = qty * price
    if notional < min_pos_usd:
        return None, "live_size_below_min"

    # Bracket prices (reuse shared order protection logic)
    bracket = compute_bracket_prices(
        symbol=symbol,
        entry_price=price,
        atr=atr,
        risk_cfg=risk_cfg,
        exec_cfg=exec_cfg,
    )
    stop_loss = bracket["stop_price"]
    take_profit = bracket["take_profit"]
    rr_ratio = bracket["rr_ratio"]

    if not validate_bracket_prices(price, stop_loss, take_profit):
        return None, "invalid_bracket_prices"

    min_rr = float(exec_cfg.get("min_rr_ratio", 1.2))
    if rr_ratio < min_rr:
        return None, "live_rr_ratio_low"

    trailing_mult = exec_cfg.get("trailing_stop_atr_mult")
    trailing_stop = float(trailing_mult) * atr_val if trailing_mult and atr_val > 0 else None

    plan = {
        "symbol": symbol,
        "price": price,
        "atr": atr_val,
        "qty": qty,
        "notional": notional,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trailing_stop": trailing_stop,
        "rr_ratio": rr_ratio,
        "use_bracket": True,
        "time_in_force": exec_cfg.get("time_in_force", "day"),
    }
    return plan, "ok"


def record_live_trade(plan: dict) -> None:
    """Persist a live trade to the daily state file."""
    state = load_live_state()
    state.spent_today_usd += float(plan.get("notional", 0.0))
    state.new_positions_today += 1
    symbol = plan.get("symbol")
    if symbol and symbol not in state.symbols_traded_today:
        state.symbols_traded_today.append(symbol)
    if symbol:
        state.symbol_last_trade[symbol] = _today_nyse()
    save_live_state(state)
