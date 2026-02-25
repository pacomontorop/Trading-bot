"""Daily risk management and sizing helpers."""

from __future__ import annotations

import copy
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


@dataclass
class DailyRiskState:
    date: str
    spent_today_usd: float = 0.0
    new_positions_today: int = 0
    symbols_traded_today: list[str] = field(default_factory=list)
    symbol_last_trade: dict[str, str] = field(default_factory=dict)
    last_trade_time: str | None = None
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "spent_today_usd": self.spent_today_usd,
            "new_positions_today": self.new_positions_today,
            "symbols_traded_today": self.symbols_traded_today,
            "symbol_last_trade": self.symbol_last_trade,
            "last_trade_time": self.last_trade_time,
            "blocked_reason": self.blocked_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], date: str) -> "DailyRiskState":
        return cls(
            date=date,
            spent_today_usd=float(data.get("spent_today_usd", 0.0)),
            new_positions_today=int(data.get("new_positions_today", 0)),
            symbols_traded_today=list(data.get("symbols_traded_today", [])),
            symbol_last_trade=dict(data.get("symbol_last_trade", {})),
            last_trade_time=data.get("last_trade_time"),
            blocked_reason=data.get("blocked_reason"),
        )


def _today_nyse() -> str:
    return datetime.now(tz=NY_TZ).strftime("%Y-%m-%d")


def _risk_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("risk", {}) or {}


def _execution_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("execution", {}) or {}


def _load_state_from_alpaca(today: str, state: DailyRiskState) -> DailyRiskState:
    """Overwrite daily counters with ground-truth data from Alpaca order history.

    Queries Alpaca for today's filled buy orders and recomputes
    ``spent_today_usd``, ``new_positions_today``, and
    ``symbols_traded_today`` so the values survive service restarts.
    ``symbol_last_trade`` (used for cooldowns) is preserved from the file.
    Falls back to the file-based state silently if the API call fails.
    """
    try:
        from broker.alpaca import get_todays_filled_buy_orders

        orders = get_todays_filled_buy_orders(today)
        if orders is None:
            log_event("RISK Alpaca order fetch failed; using file-based counters", event="RISK")
            return state

        spent = sum(
            float(getattr(o, "filled_qty", 0) or 0)
            * float(getattr(o, "filled_avg_price", 0) or 0)
            for o in orders
        )
        symbols: list[str] = list(
            dict.fromkeys(
                getattr(o, "symbol", "") for o in orders if getattr(o, "symbol", "")
            )
        )
        state.spent_today_usd = spent
        state.new_positions_today = len(symbols)
        state.symbols_traded_today = symbols
        log_event(
            f"RISK state rebuilt from Alpaca: spent={spent:.2f} "
            f"positions={len(symbols)} symbols={symbols}",
            event="RISK",
        )
    except Exception as exc:
        log_event(f"RISK Alpaca state rebuild error err={exc}; using file", event="RISK")
    return state


def load_daily_state(path: str = "data/risk_state.json") -> DailyRiskState:
    today = _today_nyse()

    # Load file primarily for symbol_last_trade (cooldown history).
    state = DailyRiskState(date=today)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle) or {}
            stored_date = payload.get("date")
            state = DailyRiskState.from_dict(payload, date=today)
            if stored_date != today:
                state.spent_today_usd = 0.0
                state.new_positions_today = 0
                state.symbols_traded_today = []
                state.blocked_reason = None
        except Exception:
            pass

    # Always override daily counters with Alpaca ground truth so restarts
    # cannot reset the spend/position counters.
    return _load_state_from_alpaca(today, state)


def save_daily_state(state: DailyRiskState, path: str = "data/risk_state.json") -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle)
    except Exception as exc:
        log_event(f"RISK state save failed err={exc}", event="RISK")


def _get_account_snapshot() -> dict | None:
    from broker import alpaca as broker

    try:  # pragma: no cover - network
        account = broker.api.get_account()
        equity = float(getattr(account, "equity", 0) or 0)
        cash = float(getattr(account, "cash", 0) or 0)
        positions = broker.list_positions()
        orders = broker.list_open_orders_today()
    except Exception as exc:  # pragma: no cover - network
        log_event(f"RISK account fetch failed err={exc}", event="RISK")
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

    buying_power = 0.0
    try:  # pragma: no cover - network
        buying_power = float(getattr(account, "buying_power", 0) or 0)
    except Exception:
        buying_power = cash

    return {
        "equity": equity,
        "cash": cash,
        "buying_power": buying_power,
        "positions": positions,
        "orders": orders,
        "total_exposure": total_exposure,
        "symbol_exposure": symbol_exposure,
    }


def _effective_daily_max(cfg: dict, snapshot: dict) -> float:
    """Return the effective daily spend cap.

    Combines the hard USD cap (``risk.daily_max_spend_usd``) with an optional
    percentage-of-buying-power cap (``risk.daily_max_spend_pct_buying_power``).

    Rules:
    - If only the USD cap is set  → use it directly.
    - If only the pct cap is set  → use ``buying_power × pct``.
    - If both are set             → use the *lower* of the two (more conservative).
    - If neither is set           → return 0.0 (no cap, use cash).
    """
    hard_usd = float(cfg.get("daily_max_spend_usd", 0) or 0)
    pct = float(cfg.get("daily_max_spend_pct_buying_power", 0) or 0)
    if pct > 0:
        buying_power = float(snapshot.get("buying_power", 0) or 0)
        pct_cap = buying_power * pct
        if hard_usd > 0:
            return min(hard_usd, pct_cap)
        return pct_cap
    return hard_usd


def _symbol_in_open_orders(symbol: str, orders) -> bool:
    try:
        return any(getattr(order, "symbol", "") == symbol for order in orders or [])
    except Exception:
        return False


def check_risk_limits(
    *,
    symbol: str,
    state: DailyRiskState,
    snapshot: dict,
    planned_spend: float,
) -> tuple[bool, list[str]]:
    cfg = _risk_cfg()
    reasons: list[str] = []

    daily_max_spend = _effective_daily_max(cfg, snapshot)
    daily_max_new_positions = int(cfg.get("daily_max_new_positions", 0))
    max_total_open_positions = int(cfg.get("max_total_open_positions", 0))
    max_exposure_pct = float(cfg.get("max_exposure_pct_equity", 1.0))
    cash_buffer_pct = float(cfg.get("cash_buffer_pct", 0.0))
    max_symbol_exposure_pct = float(cfg.get("max_symbol_exposure_pct_equity", 1.0))
    cooldown_days = int(cfg.get("symbol_cooldown_days", 0))
    skip_if_position_open = bool(cfg.get("if_position_open_skip", True))
    skip_if_order_pending = bool(cfg.get("skip_if_order_pending", True))

    equity = snapshot.get("equity", 0.0)
    cash = snapshot.get("cash", 0.0)
    positions = snapshot.get("positions", [])
    orders = snapshot.get("orders", [])

    if daily_max_spend and state.spent_today_usd >= daily_max_spend:
        reasons.append("daily_spend_exceeded")
    if daily_max_new_positions and state.new_positions_today >= daily_max_new_positions:
        reasons.append("daily_positions_exceeded")
    if max_total_open_positions and len(positions) >= max_total_open_positions:
        reasons.append("max_open_positions")
    if equity <= 0:
        reasons.append("invalid_equity")
    if cash_buffer_pct and equity > 0 and (cash / equity) < cash_buffer_pct:
        reasons.append("cash_buffer")

    if max_exposure_pct and equity > 0:
        exposure_pct = snapshot.get("total_exposure", 0.0) / equity
        if exposure_pct >= max_exposure_pct:
            reasons.append("max_exposure")

    if max_symbol_exposure_pct and equity > 0:
        symbol_exposure = snapshot.get("symbol_exposure", {}).get(symbol, 0.0)
        if symbol_exposure / equity >= max_symbol_exposure_pct:
            reasons.append("symbol_exposure")

    if skip_if_position_open:
        if any(getattr(pos, "symbol", "") == symbol for pos in positions):
            reasons.append("position_open")

    if skip_if_order_pending and _symbol_in_open_orders(symbol, orders):
        reasons.append("order_pending")

    if symbol in state.symbols_traded_today:
        reasons.append("symbol_traded_today")

    if cooldown_days:
        last_date = state.symbol_last_trade.get(symbol)
        if last_date:
            try:
                last_dt = datetime.strptime(last_date, "%Y-%m-%d")
                delta_days = (datetime.now(tz=NY_TZ) - last_dt.replace(tzinfo=NY_TZ)).days
                if delta_days < cooldown_days:
                    reasons.append("symbol_cooldown")
            except Exception:
                pass

    if planned_spend <= 0:
        reasons.append("invalid_plan_spend")

    return not reasons, reasons


def _compute_order_plan(candidate: dict, state: DailyRiskState, snapshot: dict) -> tuple[dict | None, str | None]:
    cfg = _risk_cfg()
    exec_cfg = _execution_cfg()
    price = float(candidate.get("price") or 0.0)
    atr = candidate.get("atr")
    atr_val = float(atr or 0.0)
    if price <= 0:
        return None, "invalid_price"

    equity = snapshot.get("equity", 0.0)
    cash = snapshot.get("cash", 0.0)
    if equity <= 0:
        return None, "invalid_equity"

    daily_max_spend = _effective_daily_max(cfg, snapshot)
    max_position_size = float(cfg.get("max_position_size_usd", 0))
    min_position_size = float(cfg.get("min_position_size_usd", 0))
    cash_buffer_pct = float(cfg.get("cash_buffer_pct", 0))
    max_symbol_exposure_pct = float(cfg.get("max_symbol_exposure_pct_equity", 1.0))
    slippage_buffer_pct = float(cfg.get("slippage_buffer_pct", 0.0))
    risk_pct = float(cfg.get("max_symbol_risk_pct", 0.0))
    atr_k = float(cfg.get("atr_k", 2.0))
    min_stop_pct = float(cfg.get("min_stop_pct", 0.0))

    remaining_budget = daily_max_spend - state.spent_today_usd if daily_max_spend else cash
    cash_buffer = equity * cash_buffer_pct
    cash_available = cash - cash_buffer
    base_budget = min(max_position_size or cash_available, remaining_budget, cash_available)
    if base_budget <= 0:
        return None, "cash_unavailable"

    symbol_exposure = snapshot.get("symbol_exposure", {}).get(candidate["symbol"], 0.0)
    max_symbol_budget = (equity * max_symbol_exposure_pct) - symbol_exposure if max_symbol_exposure_pct else base_budget
    size_usd = min(base_budget, max_symbol_budget, cash_available)

    if size_usd < min_position_size:
        return None, "size_below_min"

    size_usd *= max(0.0, 1 - slippage_buffer_pct)
    stop_distance = max(atr_val * atr_k, price * min_stop_pct)
    if stop_distance <= 0:
        return None, "invalid_stop_distance"

    if risk_pct <= 0:
        return None, "risk_pct_missing"

    risk_budget = equity * risk_pct
    risk_qty = int(risk_budget / stop_distance)
    max_affordable_qty = int(size_usd / price)
    qty = min(risk_qty, max_affordable_qty)
    if qty < 1:
        return None, "qty_below_one"

    notional = qty * price
    if notional <= 0:
        return None, "notional_invalid"
    if notional < min_position_size:
        return None, "size_below_min"

    use_bracket = bool(exec_cfg.get("use_bracket", False))
    if use_bracket and not is_safeguards_active():
        return None, "safeguards_inactive"
    stop_price = None
    take_profit = None
    trailing_stop = None
    rr_ratio = None
    stop_price = price - stop_distance
    if stop_price <= 0:
        return None, "invalid_stop_price"
    if use_bracket:
        bracket = compute_bracket_prices(
            symbol=candidate.get("symbol"),
            entry_price=price,
            atr=atr,
            risk_cfg=cfg,
            exec_cfg=exec_cfg,
        )
        stop_price = bracket["stop_price"]
        take_profit = bracket["take_profit"]
        rr_ratio = bracket["rr_ratio"]
        if not validate_bracket_prices(price, stop_price, take_profit):
            return None, "invalid_bracket_prices"
        min_rr = float(exec_cfg.get("min_rr_ratio", 1.2))
        if rr_ratio < min_rr:
            return None, "rr_ratio_low"

    trailing_mult = exec_cfg.get("trailing_stop_atr_mult")
    if trailing_mult and atr_val > 0:
        trailing_stop = float(trailing_mult) * atr_val

    plan = {
        "symbol": candidate["symbol"],
        "score_total": candidate["score_total"],
        "quiver_score": candidate["quiver_score"],
        "price": price,
        "atr": atr_val if atr is not None else None,
        "qty": qty,
        "notional": notional,
        "stop_loss": stop_price,
        "take_profit": take_profit,
        "trailing_stop": trailing_stop,
        "rr_ratio": rr_ratio,
        "time_in_force": exec_cfg.get("time_in_force", "day"),
        "allow_partial_fills": bool(exec_cfg.get("allow_partial_fills", True)),
        "use_bracket": use_bracket,
        "decision_trace": candidate.get("decision_trace", {}),
        "yahoo_symbol_used": candidate.get("yahoo_symbol_used"),
        "quiver_symbol_used": candidate.get("quiver_symbol_used"),
        "provider_fallback_used": candidate.get("provider_fallback_used"),
    }
    return plan, None


def plan_trades(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    if not candidates:
        return [], []

    snapshot = _get_account_snapshot()
    if snapshot is None:
        log_event("RISK abort: account snapshot missing", event="RISK")
        return [], [{"symbol": None, "reasons": ["account_snapshot_missing"]}]

    state = load_daily_state()
    planned_state = copy.deepcopy(state)
    approved: list[dict] = []
    rejections: list[dict] = []

    for candidate in candidates:
        plan, reason = _compute_order_plan(candidate, planned_state, snapshot)
        if plan is None:
            rejections.append(
                {
                    "symbol": candidate["symbol"],
                    "reasons": [reason or "plan_failed"],
                    "decision_trace": candidate.get("decision_trace", {}),
                }
            )
            continue
        ok, reasons = check_risk_limits(
            symbol=candidate["symbol"],
            state=planned_state,
            snapshot=snapshot,
            planned_spend=plan["notional"],
        )
        if not ok:
            rejections.append(
                {
                    "symbol": candidate["symbol"],
                    "reasons": reasons,
                    "decision_trace": candidate.get("decision_trace", {}),
                }
            )
            continue

        planned_state.spent_today_usd += plan["notional"]
        planned_state.new_positions_today += 1
        planned_state.symbols_traded_today.append(candidate["symbol"])
        approved.append(plan)

    return approved, rejections


def record_trade(plan: dict) -> None:
    state = load_daily_state()
    state.spent_today_usd += float(plan.get("notional", 0.0))
    state.new_positions_today += 1
    symbol = plan.get("symbol")
    if symbol and symbol not in state.symbols_traded_today:
        state.symbols_traded_today.append(symbol)
    if symbol:
        state.symbol_last_trade[symbol] = _today_nyse()
    state.last_trade_time = datetime.now(tz=NY_TZ).isoformat()
    save_daily_state(state)
