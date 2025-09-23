# executor.py

from datetime import datetime
from decimal import Decimal
import time
import threading
import pandas as pd
import yfinance as yf
import math
from broker.alpaca import api, get_current_price
from core.market_gate import is_us_equity_market_open
from broker import alpaca as broker
from core.broker import get_tick_size, round_to_tick
from libs.broker.ticks import round_stop_price, equity_tick_for, ceil_to_tick, floor_to_tick
from broker.account import get_account_equity_safe
from signals.filters import is_position_open, is_symbol_approved
from utils.state import already_executed_today, mark_executed
from core.order_utils import alpaca_order_exists
from config import STRATEGY_VER
import config
from signals.reader import get_top_shorts
from data.providers import ALLOW_STALE_EQ_WHEN_CLOSED
from utils.logger import log_event, log_dir, log_once
from utils import metrics
from utils.daily_risk import (
    register_trade_pnl,
    is_risk_limit_exceeded,
    save_equity_snapshot,
    is_equity_drop_exceeded,
    calculate_var,
    get_max_drawdown,
)
from utils.order_tracker import record_trade_result
from utils.orders import resolve_time_in_force
from utils.daily_set import DailySet
import os
import csv
import json
import gc
import utils.market_calendar as market_calendar
from utils.market_regime import compute_vix_regime, exposure_from_regime
from utils.symbols import detect_asset_class, normalize_for_yahoo

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
INVEST_STATE_FILE = os.path.join(DATA_DIR, "investment_state.json")
EXECUTED_STATE_FILE = os.path.join(DATA_DIR, "executed_symbols.json")

# Control de estado
from utils.state import StateManager
from utils.monitoring import orders_placed, update_positions_metric

state_manager = StateManager()
open_positions = state_manager.load_open_positions()
update_positions_metric(len(open_positions))
pending_opportunities = set()
pending_trades = set()

executed_symbols_today = DailySet(EXECUTED_STATE_FILE)
executed_symbols_today_lock = executed_symbols_today.lock
EVALUATED_SHORTS_FILE = os.path.join(DATA_DIR, "evaluated_shorts.json")
evaluated_shorts_today = DailySet(EVALUATED_SHORTS_FILE)
evaluated_shorts_today_lock = evaluated_shorts_today.lock
EVALUATED_LONGS_FILE = os.path.join(DATA_DIR, "evaluated_longs.json")
evaluated_longs_today = DailySet(EVALUATED_LONGS_FILE)
evaluated_longs_today_lock = evaluated_longs_today.lock
TRAILING_ERROR_FILE = os.path.join(DATA_DIR, "trailing_error_symbols.json")
trailing_error_symbols = DailySet(TRAILING_ERROR_FILE)

# Locks for thread-safe access to the remaining sets
open_positions_lock = threading.Lock()
pending_opportunities_lock = threading.Lock()
pending_trades_lock = threading.Lock()
DAILY_INVESTMENT_LIMIT_PCT = 0.50
MAX_POSITION_PCT = 0.10  # Máximo porcentaje de equity permitido por operación
DAILY_MAX_LOSS_USD = 150.0  # Límite de pérdidas diarias
STOP_PCT = float(os.getenv("STOP_PCT", "0.05"))  # Stop loss fijo por defecto 5%
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))  # Riesgo máximo por operación 1%

# Mutex por símbolo para evitar condiciones de carrera
_symbol_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()


def _get_symbol_lock(symbol: str) -> threading.Lock:
    with _global_lock:
        if symbol not in _symbol_locks:
            _symbol_locks[symbol] = threading.Lock()
        return _symbol_locks[symbol]


# Generación determinística de client_order_id
import hashlib
import datetime as _dt


def make_client_order_id(symbol: str, side: str, nonce: str | None = None) -> str:
    session = _dt.datetime.utcnow().strftime("%Y%m%d")
    raw = f"{session}:{symbol}:{side}:{nonce or '0'}"
    return "BOT-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def _cfg_risk(cfg):
    r = (cfg or {}).get("risk", {})
    return {
        "max_daily_loss_pct": float(r.get("max_daily_loss_pct", 0.7)),
        "max_symbol_risk_pct": float(r.get("max_symbol_risk_pct", 0.35)),
        "atr_k": float(r.get("atr_k", 2.0)),
        "min_stop_pct": float(r.get("min_stop_pct", 0.05)),
        "min_trailing_pct": float(r.get("min_trailing_pct", 0.005)),
        "max_trailing_pct": float(r.get("max_trailing_pct", 0.05)),
        "allow_fractional": bool(r.get("allow_fractional", True)),
        "min_equity_usd": float(r.get("min_equity_usd", 0.0)),
    }


def _equity_guard(equity: float, cfg, key: str) -> bool:
    r = (cfg or {}).get("risk", {})
    min_equity = float(r.get("min_equity_usd", 0.0))
    if equity is None or equity <= 0:
        log_once(
            f"equity_guard_zero_{key}",
            "RISK: ❌ equity inválido (0). Trading deshabilitado hasta recuperar equity.",
            min_interval_sec=300,
        )
        return False
    if min_equity > 0 and equity < min_equity:
        log_once(
            f"equity_guard_min_{key}",
            (
                f"RISK: ❌ equity actual {equity:.2f} < mínimo requerido {min_equity:.2f}. "
                "Trading deshabilitado."
            ),
            min_interval_sec=300,
        )
        return False
    return True


def _apply_event_and_cutoff_policies(symbol: str, sizing_notional: float, cfg) -> tuple[bool, float, str]:
    """Return (allowed, adjusted_notional, reason) after applying event and cutoff rules."""
    mkt = (cfg or {}).get("market", {})
    avoid_days = int(mkt.get("avoid_earnings_days", 3))
    event_mode = (mkt.get("event_block_mode") or "reduce").lower()
    reduce_frac = float(mkt.get("event_reduce_fraction", 0.5))
    consider_div = bool(mkt.get("consider_dividends", True))
    consider_guid = bool(mkt.get("consider_guidance", True))
    cutoff_min = int(mkt.get("avoid_last_minutes", 20))

    # Cutoff fin de sesión
    mins = market_calendar.minutes_to_close(None)
    if cutoff_min > 0 and mins <= cutoff_min:
        return (False, 0.0, f"cutoff_last_{cutoff_min}m")

    # Eventos
    has_event = market_calendar.earnings_within(symbol, avoid_days)
    if has_event:
        if event_mode == "block":
            return (False, 0.0, f"event_block_{avoid_days}d")
        return (True, sizing_notional * reduce_frac, f"event_reduce_{reduce_frac:.2f}")

    return (True, sizing_notional, "ok")


def compute_chandelier_trail(price: float, atr: float, cfg) -> float:
    """Compute Chandelier-style trailing distance in dollars for a long position."""
    r = (cfg or {}).get("risk", {})
    atr_k = float(r.get("atr_k", 2.0))
    min_tr = float(r.get("min_trailing_pct", 0.005))
    max_tr = float(r.get("max_trailing_pct", 0.05))
    atr = 0.0 if atr is None else float(atr)
    price = float(price or 0.0)
    if price <= 0:
        return 0.0
    trail = max(min_tr * price, atr_k * atr)
    trail = min(trail, max_tr * price)
    return max(trail, 0.0)


def compute_partial_take_profit(
    entry_price: float,
    stop_distance: float,
    cfg,
    tick: float | None = None,
) -> float | None:
    """Return partial take-profit price or ``None`` if disabled."""
    ex = (cfg or {}).get("exits", {})
    if not bool(ex.get("use_partial_take_profit", True)):
        return None
    R = float(ex.get("partial_tp_at_R", 1.5))
    if stop_distance is None or stop_distance <= 0:
        return None
    price = entry_price + R * stop_distance
    if tick is not None and tick > 0:
        return round_to_tick(price, tick, mode="up")
    return round(price, 2)


def should_use_combined_bracket(cfg, broker_module) -> bool:
    """Determine whether to place TP and trailing in the same bracket."""
    ex = (cfg or {}).get("exits", {})
    if not bool(ex.get("allow_tp_and_trailing_same_bracket", False)):
        return False
    supports = getattr(broker_module, "supports_bracket_trailing", None)
    return bool(callable(supports) and supports())


_EXPOSURE_CACHE: dict[str, float] = {}


def _tick_rounding_enabled(cfg) -> bool:
    risk_cfg = (cfg or {}).get("risk", {})
    return bool(risk_cfg.get("enforce_tick_rounding", True))


def _apply_tick_rounding(
    *,
    symbol: str,
    side: str,
    entry_price: float | None,
    asset_class: str | None,
    stop_price: float | None = None,
    take_profit: float | None = None,
    trail_price: float | None = None,
    cfg=None,
):
    tick = get_tick_size(symbol, asset_class, entry_price)
    lower_side = (side or "").lower()
    tp_mode = "up" if lower_side == "buy" else "down"
    round_asset_class = asset_class or "us_equity"
    if round_asset_class == "equity":
        round_asset_class = "us_equity"
    stop_side = "SELL" if lower_side == "buy" else "BUY"
    rounded_stop_dec = (
        round_stop_price(
            symbol,
            stop_side,
            stop_price,
            asset_class=round_asset_class,
            tick_override=tick,
        )
        if stop_price is not None
        else None
    )
    rounded_stop = float(rounded_stop_dec) if rounded_stop_dec is not None else None
    rounded_tp = round_to_tick(take_profit, tick, mode=tp_mode)
    rounded_trail = round_to_tick(trail_price, tick)

    if (
        lower_side == "buy"
        and entry_price not in (None, 0)
        and rounded_stop is not None
        and tick > 0
        and rounded_stop >= entry_price
    ):
        adjusted_dec = round_stop_price(
            symbol,
            stop_side,
            entry_price - tick,
            asset_class=round_asset_class,
            tick_override=tick,
        )
        adjusted = float(adjusted_dec) if adjusted_dec is not None else None
        if adjusted is None or adjusted <= 0:
            log_event(
                f"RISK {symbol}: ❌ stop>=entry tras redondeo, se omite stop", event="RISK"
            )
            rounded_stop = None
        else:
            log_event(
                f"RISK {symbol}: stop ajustado a {adjusted:.4f} tras redondeo", event="RISK"
            )
            rounded_stop = adjusted

    if (
        lower_side == "sell"
        and entry_price not in (None, 0)
        and rounded_stop is not None
        and tick > 0
        and rounded_stop <= entry_price
    ):
        adjusted_dec = round_stop_price(
            symbol,
            stop_side,
            entry_price + tick,
            asset_class=round_asset_class,
            tick_override=tick,
        )
        adjusted = float(adjusted_dec) if adjusted_dec is not None else None
        if adjusted is None or adjusted <= 0:
            log_event(
                f"RISK {symbol}: ❌ stop<=entry tras redondeo, se omite stop", event="RISK"
            )
            rounded_stop = None
        else:
            log_event(
                f"RISK {symbol}: stop ajustado a {adjusted:.4f} tras redondeo", event="RISK"
            )
            rounded_stop = adjusted

    return tick, rounded_stop, rounded_tp, rounded_trail


def get_market_exposure_factor(cfg) -> float:
    """Return market-wide exposure factor based on VIX regime.

    The value is cached per day; ``compute_vix_regime`` maintains its own TTL.
    """
    today = _dt.datetime.utcnow().date()
    cached = _EXPOSURE_CACHE.get(str(today))
    if cached is not None:
        return cached

    regime_info = compute_vix_regime(cfg)
    factor = exposure_from_regime(cfg, regime_info["regime"])
    mkt = (cfg or {}).get("market", {})
    min_e = float(mkt.get("min_exposure", 0.6))
    max_e = float(mkt.get("max_exposure", 1.0))
    exposure = max(min_e, min(max_e, factor))

    log_event(
        "REGIME: {} VIX={} pctiles={} composite={:.1f} -> exposure={:.2f}".format(
            regime_info["regime"],
            regime_info.get("today"),
            regime_info.get("pctiles"),
            regime_info.get("composite", 0.0),
            exposure,
        )
    )

    _EXPOSURE_CACHE.clear()
    _EXPOSURE_CACHE[str(today)] = exposure
    return exposure


def calculate_position_size_risk_based(
    symbol: str,
    price: float,
    atr: float | None,
    equity: float,
    cfg,
    market_exposure_factor: float = 1.0,
    daily_realized_loss_pct: float = 0.0,
):
    """
    Devuelve: {shares, notional, stop_distance, risk_budget, reason}
    Regla:
      stop_distance = max(atr_k * ATR, min_stop_pct * price)
      risk_budget  = equity * max_symbol_risk_pct / 100
      shares       = risk_budget / stop_distance   (fraccional si allow_fractional)
      notional     = shares * price
      caps         = min(notional, 10% del equity) y multiplicar por market_exposure_factor
    """
    r = _cfg_risk(cfg)

    asset_class = detect_asset_class(symbol)
    if asset_class != "equity":
        return {
            "shares": 0,
            "notional": 0.0,
            "stop_distance": None,
            "risk_budget": 0.0,
            "reason": f"unsupported_asset_class_{asset_class}",
        }

    equity = float(equity or 0.0)
    price = float(price or 0.0)
    min_equity = float(r.get("min_equity_usd", 0.0))

    if equity <= 0 or price <= 0:
        return {
            "shares": 0,
            "notional": 0.0,
            "stop_distance": None,
            "risk_budget": 0.0,
            "reason": "invalid_inputs",
        }

    if min_equity > 0 and equity < min_equity:
        return {
            "shares": 0,
            "notional": 0.0,
            "stop_distance": None,
            "risk_budget": 0.0,
            "reason": f"equity_below_min_{min_equity:.0f}",
        }

    if daily_realized_loss_pct >= r["max_daily_loss_pct"]:
        return {
            "shares": 0,
            "notional": 0.0,
            "stop_distance": None,
            "risk_budget": 0.0,
            "reason": f"daily_loss_limit_reached_{daily_realized_loss_pct:.2f}%",
        }

    atr_val = float(atr or 0.0)
    stop_distance = max(r["atr_k"] * atr_val, r["min_stop_pct"] * price)
    stop_distance = max(stop_distance, 1e-6)

    risk_budget = equity * (r["max_symbol_risk_pct"] / 100.0)
    raw_shares = risk_budget / max(stop_distance, 1e-6)

    if r["allow_fractional"]:
        shares = max(raw_shares, 0.0)
    else:
        shares = math.floor(raw_shares)

    notional = shares * price

    per_symbol_cap = 0.10 * equity
    if notional > per_symbol_cap:
        if r["allow_fractional"]:
            shares = per_symbol_cap / price
        else:
            shares = math.floor(per_symbol_cap / price)
        notional = shares * price

    exposure = float(market_exposure_factor or 1.0)
    shares *= exposure
    if not r["allow_fractional"]:
        shares = math.floor(shares)
    shares = max(shares, 0.0)
    notional = shares * price

    if notional <= 0 or shares <= 0:
        return {
            "shares": 0,
            "notional": 0.0,
            "stop_distance": stop_distance,
            "risk_budget": risk_budget,
            "reason": "size_zero_after_caps_or_exposure",
        }

    return {
        "shares": shares,
        "notional": notional,
        "stop_distance": stop_distance,
        "risk_budget": risk_budget,
        "reason": "ok",
    }


def _load_investment_state():
    """Load persisted daily investment state from disk."""
    try:
        with open(INVEST_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        day = datetime.strptime(data.get("date", ""), "%Y-%m-%d").date()
        invested = float(data.get("invested", 0.0))
        pnl = float(data.get("pnl", 0.0))
        today = datetime.utcnow().date()
        if day != today:
            day, invested, pnl = today, 0.0, 0.0
    except Exception:
        day, invested, pnl = datetime.utcnow().date(), 0.0, 0.0
    return day, invested, pnl


def _save_investment_state():
    """Persist current daily investment state to disk."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(INVEST_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": _last_investment_day.isoformat(),
                    "invested": _total_invested_today,
                    "pnl": _realized_pnl_today,
                },
                f,
            )
    except Exception:
        pass


_last_investment_day, _total_invested_today, _realized_pnl_today = _load_investment_state()
_last_equity_snapshot = None

quiver_signals_log = {}
# Store entry price, quantity and entry time for open positions to calculate PnL when they close
entry_data = {}


def register_open_position(symbol: str) -> None:
    with open_positions_lock:
        open_positions.add(symbol)
    state_manager.add_open_position(symbol)
    update_positions_metric(len(open_positions))


def unregister_open_position(symbol: str) -> None:
    with open_positions_lock:
        open_positions.discard(symbol)
    state_manager.remove_open_position(symbol)
    update_positions_metric(len(open_positions))

def update_risk_limits():
    """Adjust risk limits based on recent VaR and drawdown."""
    global MAX_POSITION_PCT, DAILY_INVESTMENT_LIMIT_PCT
    var = calculate_var(window=30, confidence=0.95)
    drawdown = get_max_drawdown(window=30)
    if var > 0.05 or drawdown < -10:
        MAX_POSITION_PCT = 0.05
        DAILY_INVESTMENT_LIMIT_PCT = 0.25
    elif var > 0.03 or drawdown < -5:
        MAX_POSITION_PCT = 0.08
        DAILY_INVESTMENT_LIMIT_PCT = 0.40
    else:
        MAX_POSITION_PCT = 0.10
        DAILY_INVESTMENT_LIMIT_PCT = 0.50


def reset_daily_investment():
    global _total_invested_today, _last_investment_day, _realized_pnl_today
    today = datetime.utcnow().date()
    if today != _last_investment_day:
        _total_invested_today = 0.0
        _realized_pnl_today = 0.0
        _last_investment_day = today
        executed_symbols_today.clear()
        # Clear intraday caches to avoid unbounded growth
        quiver_signals_log.clear()
        gc.collect()
        update_risk_limits()
        _save_investment_state()

def add_to_invested(amount):
    global _total_invested_today
    _total_invested_today += amount
    _save_investment_state()

def invested_today_usd():
    return _total_invested_today


def calculate_investment_amount(score: int, equity: float, cfg) -> float:
    """Legacy fixed sizing. Avoid using in production."""
    log_event("⚠️ calculate_investment_amount invoked — using legacy sizing")
    base_allocation = 2000 + 10 * score
    base_allocation = max(2000, min(3000, base_allocation))
    per_symbol_cap = 0.10 * equity
    return min(base_allocation, per_symbol_cap)


def get_adaptive_trail_price(symbol, window: int = 14):
    """Calcula un ``trail_price`` dinámico utilizando el ATR de ``window`` días."""
    try:
        asset_class = detect_asset_class(symbol)
        if asset_class != "equity":
            price = get_current_price(symbol)
            if not price:
                return 0.0
            return round(float(price) * 0.01, 2)

        yf_symbol = normalize_for_yahoo(symbol)
        hist = yf.download(yf_symbol, period="21d", interval="1d", progress=False)
        if hist.empty or not {"High", "Low", "Close"}.issubset(hist.columns):
            raise ValueError("Datos insuficientes")

        high = hist["High"]
        low = hist["Low"]
        close = hist["Close"]

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(window).mean().iloc[-1]

        current_price = close.iloc[-1]
        atr_pct = atr / current_price if current_price else 0
        atr_pct = min(max(float(atr_pct), 0.005), 0.05)
        result = round(float(current_price) * atr_pct, 2)
        del hist
        gc.collect()
        return result
    except Exception as e:
        if symbol not in trailing_error_symbols:
            print(f"⚠️ Error calculando trail adaptativo para {symbol}: {e}")
            trailing_error_symbols.add(symbol)
        fallback_price = get_current_price(symbol)
        if not fallback_price:
            return 0.0
        return round(float(fallback_price) * 0.015, 2)


def update_trailing_stop(symbol, order_id=None, trail_price=None, trail_percent=None, side=None):
    """Actualiza un trailing stop existente con nueva distancia."""
    try:
        order_side = side
        if order_id is None:
            orders = api.list_orders(status="open")
            for o in orders:
                if o.symbol == symbol and getattr(o, "type", "") == "trailing_stop":
                    order_id = o.id
                    order_side = getattr(o, "side", order_side)
                    break
        if not order_id:
            return False
        if order_side is None:
            try:
                existing = api.get_order(order_id)
                order_side = getattr(existing, "side", order_side)
            except Exception:
                order_side = side
        if _tick_rounding_enabled(config._policy) and trail_price is not None:
            _, _, _, rounded = _apply_tick_rounding(
                symbol=symbol,
                side=order_side or "buy",
                entry_price=None,
                asset_class=detect_asset_class(symbol),
                trail_price=trail_price,
                cfg=config._policy,
            )
            trail_price = rounded
        api.replace_order(order_id, trail_price=trail_price, trail_percent=trail_percent)
        log_event(f"🔁 Trailing stop actualizado para {symbol}")
        return True
    except Exception as e:
        log_event(f"❌ Error actualizando trailing stop para {symbol}: {e}")
        return False


def update_stop_order(symbol, order_id=None, stop_price=None, limit_price=None, side=None):
    """Actualiza una orden stop o stop-limit existente."""
    try:
        if order_id is None:
            orders = api.list_orders(status="open")
            for o in orders:
                if o.symbol == symbol and getattr(o, "type", "") in ("stop", "stop_limit"):
                    order_id = o.id
                    side = getattr(o, "side", side)
                    break
        if not order_id:
            return False
        if side is None:
            try:
                existing = api.get_order(order_id)
                side = getattr(existing, "side", None)
            except Exception:
                side = None

        params = {}
        mode = "down" if (side or "").lower() == "sell" else "up"
        tick_rounding = _tick_rounding_enabled(config._policy)
        tick = None
        asset_cls = detect_asset_class(symbol)
        if tick_rounding:
            basis = stop_price if stop_price is not None else limit_price
            tick = get_tick_size(symbol, asset_cls, basis)
        rounded_stop_dec = None
        if stop_price is not None:
            if tick_rounding:
                round_asset_class = asset_cls if asset_cls != "equity" else "us_equity"
                rounded_stop_dec = round_stop_price(
                    symbol,
                    side or "",
                    stop_price,
                    asset_class=round_asset_class,
                    tick_override=tick,
                )
                params["stop_price"] = (
                    float(rounded_stop_dec)
                    if rounded_stop_dec is not None
                    else stop_price
                )
            else:
                params["stop_price"] = stop_price
        if limit_price is not None:
            params["limit_price"] = (
                round_to_tick(limit_price, tick, mode=mode) if tick else limit_price
            )
        api.replace_order(order_id, **params)
        log_event(f"🔁 Stop actualizado para {symbol}")
        return True
    except Exception as e:
        message = str(e).lower()
        if "sub-penny" in message and rounded_stop_dec is not None:
            try:
                tick_dec = Decimal(str(tick)) if tick else equity_tick_for(rounded_stop_dec)
                adjust_dec = (
                    ceil_to_tick(rounded_stop_dec, tick_dec)
                    if (side or "").lower() == "sell"
                    else floor_to_tick(rounded_stop_dec, tick_dec)
                )
                params["stop_price"] = float(adjust_dec)
                api.replace_order(order_id, **params)
                log_event(
                    f"🔁 Stop ajustado por sub-penny para {symbol}",
                    event="ORDER",
                )
                return True
            except Exception as inner:
                log_event(f"❌ Ajuste sub-penny falló para {symbol}: {inner}")
        log_event(f"❌ Error actualizando stop para {symbol}: {e}")
        return False

def wait_for_order_fill(order_id, symbol, timeout=60):
    print(f"⌛ Empezando espera para orden {order_id} de {symbol}", flush=True)
    start = time.time()
    while time.time() - start < timeout:
        try:
            order = api.get_order(order_id)
            print(
                f"⌛ Orden {order_id} para {symbol} estado actual: {order.status}",
                flush=True,
            )
            if order.status == "filled":
                # Register entry price when the initial market order is filled
                if order.type == "market":
                    try:
                        entry_time = getattr(order, "filled_at", datetime.utcnow())
                        if isinstance(entry_time, str):
                            try:
                                entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                            except Exception:
                                entry_time = datetime.utcnow()
                        prev = entry_data.get(symbol, (None, None, None, None))
                        entry_data[symbol] = (
                            float(order.filled_avg_price),
                            float(order.qty),
                            entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                            prev[3],
                        )
                    except Exception:
                        pass

                # Calculate realized PnL when a closing order completes
                if order.type in ("trailing_stop", "limit"):
                    fill_price = float(getattr(order, "filled_avg_price", 0))
                    qty = float(order.qty)
                    avg_entry, _, date_in, _ = entry_data.get(symbol, (None, None, None, None))
                    if avg_entry is not None:
                        if order.side == "sell":
                            pnl = (fill_price - avg_entry) * qty
                        else:
                            pnl = (avg_entry - fill_price) * qty
                        log_event(f"💰 PnL realized for {symbol}: {pnl:.2f} USD")
                        pnl_file = os.path.join(log_dir, "pnl.log")
                        os.makedirs(log_dir, exist_ok=True)
                        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                        with open(pnl_file, "a", encoding="utf-8") as pf:
                            pf.write(f"[{timestamp}] {symbol} {pnl:.2f}\n")

                        # Save closed trade details
                        exit_time = getattr(order, "filled_at", datetime.utcnow())
                        if isinstance(exit_time, str):
                            try:
                                exit_time = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
                            except Exception:
                                exit_time = datetime.utcnow()
                        exit_time_str = exit_time.strftime("%Y-%m-%d %H:%M:%S")

                        os.makedirs(DATA_DIR, exist_ok=True)
                        trades_path = os.path.join(DATA_DIR, "trades.csv")
                        file_exists = os.path.exists(trades_path)
                        signals = "|".join(quiver_signals_log.get(symbol, []))
                        with open(trades_path, "a", newline="", encoding="utf-8") as tf:
                            writer = csv.writer(tf)
                            if not file_exists:
                                writer.writerow([
                                    "symbol",
                                    "entry_price",
                                    "exit_price",
                                    "qty",
                                    "pnl_usd",
                                    "date_in",
                                    "date_out",
                                    "signals",
                                ])
                            writer.writerow([
                                symbol,
                                avg_entry,
                                fill_price,
                                qty,
                                round(pnl, 2),
                                date_in,
                                exit_time_str,
                                signals,
                            ])

                        record_trade_result(
                            symbol,
                            avg_entry,
                            fill_price,
                            qty,
                            "long" if order.side == "sell" else "short",
                            (date_in.split(" ")[0] if date_in else timestamp.split(" ")[0]),
                            exit_time_str.split(" ")[0],
                        )

                        # Remove cached data to free memory once trade is closed
                        entry_data.pop(symbol, None)
                        quiver_signals_log.pop(symbol, None)
                        global _realized_pnl_today
                        _realized_pnl_today += pnl
                        register_trade_pnl(symbol, pnl)
                        _save_investment_state()
                return True
            elif order.status in ["canceled", "rejected"]:
                reason = getattr(order, "reject_reason", "Sin motivo")
                if order.status == "canceled":
                    print(
                        f"ℹ️ Orden {order_id} para {symbol} cancelada: {reason}",
                        flush=True,
                    )
                    log_event(
                        f"ℹ️ Orden cancelada para {symbol}: {reason}"
                    )
                    return True
                else:
                    print(
                        f"❌ Orden {order_id} para {symbol} rechazada: {reason}",
                        flush=True,
                    )
                    log_event(
                        f"❌ Falló la orden para {symbol}: {order.status} - {reason}"
                    )
                    return False
        except Exception as e:
            log_event(
                f"❌ Error verificando estado de orden {order_id} para {symbol}: {e}"
            )
        time.sleep(1)
    print(
        f"⚠️ Timeout esperando ejecución de orden {order_id} para {symbol}",
        flush=True,
    )
    log_event(f"⚠️ Timeout esperando fill para {symbol}")
    return False

def legacy_place_order_with_trailing_stop(symbol, sizing, trail_percent=1.0):
    global _last_equity_snapshot
    client_order_id = make_client_order_id(symbol, "BUY", STRATEGY_VER)
    if already_executed_today(symbol) or alpaca_order_exists(client_order_id):
        log_event(f"⏩ Orden duplicada para {symbol}, se omite")
        return False
    reset_daily_investment()
    today = datetime.utcnow().date()
    if _last_equity_snapshot != today:
        save_equity_snapshot()
        _last_equity_snapshot = today
    if is_equity_drop_exceeded(5.0):
        log_event(
            "🛑 STOP automático: equity cayó más de 5% respecto a ayer. No se operará hoy."
        )
        return False
    if is_risk_limit_exceeded():
        log_event("⚠️ Límite de pérdidas diarias alcanzado. No se operará más hoy.")
        return False
    if _realized_pnl_today < -DAILY_MAX_LOSS_USD:
        log_event(
            f"⛔ Límite diario de pérdidas alcanzado: {_realized_pnl_today:.2f} USD"
        )
        return False
    amount_usd = float(sizing.get("notional", 0.0))
    shares = float(sizing.get("shares", 0.0))
    stop_distance = sizing.get("stop_distance")
    print(f"\n🚀 Iniciando proceso de compra para {symbol} por ${amount_usd}...")
    try:
        if not is_symbol_approved(symbol, 0, config._policy):
            print(f"❌ {symbol} no aprobado para compra según criterios de análisis.")
            return False

        print(f"✅ {symbol} pasó todos los filtros iniciales. Obteniendo señales finales...")

        from signals.quiver_utils import (
            get_all_quiver_signals,
            score_quiver_signals,
            QUIVER_APPROVAL_THRESHOLD,
            get_adaptive_take_profit,
        )
        quiver_signals = get_all_quiver_signals(symbol)
        quiver_score = score_quiver_signals(quiver_signals)
        quiver_signals_log[symbol] = [k for k, v in quiver_signals.items() if v]

        equity = get_account_equity_safe()
        if not _equity_guard(equity, config._policy, "legacy_long"):
            return False

        account = api.get_account()
        buying_power = float(getattr(account, "buying_power", getattr(account, "cash", 0)))
        r_cfg = _cfg_risk(config._policy)

        try:
            asset = api.get_asset(symbol)
            if not getattr(asset, "tradable", True):
                print(f"⛔ {symbol} no es tradable en Alpaca.", flush=True)
                return False
            is_fractionable = getattr(asset, "fractionable", False)
        except Exception as e:
            print(f"❌ Error obteniendo información de {symbol}: {e}", flush=True)
            return False

        # Nueva excepción: si Quiver score es muy alto (> 10), ignorar límite
        if invested_today_usd() + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT and quiver_score < 10:
            print("⛔ Límite de inversión alcanzado para hoy y Quiver score < 10.")
            return False
        elif invested_today_usd() + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT:
            print(f"⚠️ {symbol} excede límite pero Quiver score = {quiver_score} ➜ Se permite excepcionalmente.")

        with open_positions_lock, executed_symbols_today_lock:
            if symbol in open_positions or symbol in executed_symbols_today:
                print(f"⚠️ {symbol} ya ejecutado o con posición abierta.")
                return False

        if is_position_open(symbol):
            print(f"⚠️ Ya hay una posición abierta en {symbol}. No se realiza nueva compra.")
            return False

        current_price = get_current_price(symbol)
        if not current_price:
            print(f"❌ Precio no disponible para {symbol}")
            return False

        if not r_cfg["allow_fractional"] and shares < 1:
            log_event(f"SIZE {symbol}: ❌ fracciones no permitidas y shares<1")
            return False

        qty = shares if (is_fractionable and r_cfg["allow_fractional"]) else int(shares)
        cost = qty * current_price
        if cost > buying_power:
            print(
                f"⛔ Fondos insuficientes para comprar {symbol}: requieren {cost}, disponible {buying_power}",
                flush=True,
            )
            return False

        print(
            f"🛒 Orden de compra -> {symbol} {qty:.4f}×${current_price:.2f} (~${cost:.2f})",
            flush=True,
        )
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            type='market',
            time_in_force=resolve_time_in_force(qty),
        )
        orders_placed.inc()
        metrics.inc("ordered")
        print(
            f"📨 Orden enviada: ID {order.id}, estado inicial {order.status}",
            flush=True,
        )
        log_event(
            "placed market order",
            event="ORDER",
            symbol=symbol,
            qty=f"{qty:.4f}",
            notional=f"{cost:.2f}",
        )
        entry_data[symbol] = (None, shares, None, stop_distance)
        print(
            "⌛ Esperando a que se rellene la orden...",
            flush=True,
        )
        if not wait_for_order_fill(order.id, symbol):
            return False
        entry_price, filled_qty, _, stop_dist = entry_data.get(
            symbol, (current_price, qty, None, stop_distance)
        )
        qty = float(filled_qty)
        asset_class = detect_asset_class(symbol)
        take_profit = get_adaptive_take_profit(symbol, entry_price, quiver_score)
        if take_profit:
            if _tick_rounding_enabled(config._policy):
                _, _, take_profit, _ = _apply_tick_rounding(
                    symbol=symbol,
                    side="buy",
                    entry_price=entry_price,
                    asset_class=asset_class,
                    take_profit=take_profit,
                    cfg=config._policy,
                )
            print(
                f"🎯 Colocando take profit para {symbol} en ${take_profit:.2f}",
                flush=True,
            )
            tp_order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side='sell',
                type='limit',
                time_in_force=resolve_time_in_force(qty),
                limit_price=take_profit,
            )
            orders_placed.inc()
            threading.Thread(
                target=wait_for_order_fill,
                args=(tp_order.id, symbol, 7 * 24 * 3600),
                daemon=True,
            ).start()

        trail_price = max(get_adaptive_trail_price(symbol), entry_price * STOP_PCT)
        if _tick_rounding_enabled(config._policy):
            _, _, _, trail_price = _apply_tick_rounding(
                symbol=symbol,
                side="buy",
                entry_price=entry_price,
                asset_class=asset_class,
                trail_price=trail_price,
                cfg=config._policy,
            )
        trail_order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='trailing_stop',
            time_in_force=resolve_time_in_force(qty),
            trail_price=trail_price
        )
        orders_placed.inc()
        threading.Thread(
            target=wait_for_order_fill,
            args=(trail_order.id, symbol, 7 * 24 * 3600),
            daemon=True,
        ).start()

        register_open_position(symbol)
        add_to_invested(amount_usd)
        executed_symbols_today.add(symbol)
        mark_executed(symbol)
        with pending_trades_lock:
            pending_trades.add(f"{symbol}: {qty} unidades — ${amount_usd:.2f}")

        log_event(
            f"✅ Compra y trailing stop colocados para {symbol}: {qty} unidades por {amount_usd:.2f} USD (Quiver score: {quiver_score})"
        )
        return True

    except Exception as e:
        print(f"❌ Falló la orden para {symbol}: {e}", flush=True)
        log_event(f"❌ Falló la orden para {symbol}: {e}")
        return False


def place_short_order_with_trailing_buy(symbol, sizing, trail_percent=1.0):
    reset_daily_investment()
    if is_risk_limit_exceeded():
        log_event("⚠️ Límite de pérdidas diarias alcanzado. No se operará más hoy.")
        return
    if _realized_pnl_today < -DAILY_MAX_LOSS_USD:
        log_event(
            f"⛔ Límite diario de pérdidas alcanzado: {_realized_pnl_today:.2f} USD"
        )
        return
    amount_usd = float(sizing.get("notional", 0.0))
    shares = float(sizing.get("shares", 0.0))
    stop_distance = sizing.get("stop_distance")
    print(f"\n🚀 Iniciando proceso de short para {symbol} por ${amount_usd}...")
    try:
        if not is_symbol_approved(symbol, 0, config._policy):
            print(f"❌ {symbol} no aprobado para short según criterios de análisis.")
            return

        print(f"✅ {symbol} pasó filtros iniciales para short. Obteniendo señales finales...")

        from signals.quiver_utils import get_all_quiver_signals
        quiver_signals_log[symbol] = [
            k for k, v in get_all_quiver_signals(symbol).items() if v
        ]

        equity = get_account_equity_safe()
        if not _equity_guard(equity, config._policy, "short"):
            return

        if invested_today_usd() + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT:
            print("⛔ Límite de inversión alcanzado para hoy.")
            return

        with open_positions_lock, executed_symbols_today_lock:
            if symbol in open_positions or symbol in executed_symbols_today:
                print(f"⚠️ {symbol} ya ejecutado o con posición abierta.")
                return

        if is_position_open(symbol):
            print(f"⚠️ Ya hay una posición abierta en {symbol}. No se realiza nuevo short.")
            return

        current_price = get_current_price(symbol)
        if not current_price:
            print(f"❌ Precio no disponible para {symbol}")
            return

        qty = int(shares)
        if qty <= 0:
            log_event(f"SIZE {symbol}: ❌ fracciones no permitidas y shares<1")
            return

        print(
            f"📉 Enviando orden SHORT para {symbol} por ${amount_usd} → {qty} unidades a ${current_price:.2f} cada una.")
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='market',
            time_in_force=resolve_time_in_force(qty)
        )
        orders_placed.inc()
        metrics.inc("ordered")

        entry_data[symbol] = (None, shares, None, stop_distance)
        if not wait_for_order_fill(order.id, symbol):
            return
        entry_price, filled_qty, _, stop_dist = entry_data.get(
            symbol, (current_price, qty, None, stop_distance)
        )
        qty = float(filled_qty)

        trail_price = max(get_adaptive_trail_price(symbol), entry_price * STOP_PCT)
        if _tick_rounding_enabled(config._policy):
            _, _, _, trail_price = _apply_tick_rounding(
                symbol=symbol,
                side="sell",
                entry_price=entry_price,
                asset_class=detect_asset_class(symbol),
                trail_price=trail_price,
                cfg=config._policy,
            )
        trail_order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            type='trailing_stop',
            time_in_force=resolve_time_in_force(qty),
            trail_price=trail_price
        )
        orders_placed.inc()
        threading.Thread(
            target=wait_for_order_fill,
            args=(trail_order.id, symbol, 7 * 24 * 3600),
            daemon=True,
        ).start()

        register_open_position(symbol)
        add_to_invested(amount_usd)
        executed_symbols_today.add(symbol)
        with pending_trades_lock:
            pending_trades.add(f"{symbol} SHORT: {qty} unidades — ${amount_usd:.2f}")

        log_event(
            "short order placed",
            event="ORDER",
            symbol=symbol,
            qty=f"{qty:.4f}",
            notional=f"{amount_usd:.2f}",
        )
        return True

    except Exception as e:
        print(f"❌ Falló la orden para {symbol}: {e}", flush=True)
        log_event(
            f"Falló la orden: {e}",
            event="ERROR",
            symbol=symbol,
        )
        return False

def short_scan():
    print("🌀 short_scan iniciado.", flush=True)
    while True:
        evaluated_shorts_today.reset_if_new_day()
        market_open = is_us_equity_market_open()
        if not market_open and not ALLOW_STALE_EQ_WHEN_CLOSED:
            log_event(
                (
                    "EQUITY_SCAN skipped reason=market_closed "
                    f"mode=afterhours_allowed={ALLOW_STALE_EQ_WHEN_CLOSED}"
                ),
                event="SCAN",
            )
            time.sleep(60)
            continue
        mode = "regular" if market_open else "afterhours"
        log_event(
            (
                f"EQUITY_SCAN running mode={mode}" +
                (f" (stale_ok={ALLOW_STALE_EQ_WHEN_CLOSED})" if mode == "afterhours" else "")
            ),
            event="SCAN",
        )
        print("🔍 Buscando oportunidades en corto...", flush=True)
        shorts = get_top_shorts(min_criteria=6, verbose=True, exclude=evaluated_shorts_today)
        log_event(f"🔻 {len(shorts)} oportunidades encontradas para short (máx 5 por ciclo)")
        MAX_SHORTS_PER_CYCLE = 1
        if len(shorts) > MAX_SHORTS_PER_CYCLE:
            print(
                f"⚠️ Hay más de {MAX_SHORTS_PER_CYCLE} shorts válidos. Se ejecutan solo las primeras.",
                flush=True,
            )
        for symbol, score, origin, current_price, current_atr in shorts[:MAX_SHORTS_PER_CYCLE]:
            with executed_symbols_today_lock, evaluated_shorts_today_lock:
                already_executed = symbol in executed_symbols_today
                already_evaluated = symbol in evaluated_shorts_today
            if already_executed or already_evaluated:
                motivo = "ejecutado" if already_executed else "evaluado"
                print(f"⏩ {symbol} ya {motivo} hoy. Se omite.", flush=True)
                continue
            evaluated_shorts_today.add(symbol)
            try:
                asset = api.get_asset(symbol)
                if getattr(asset, "shortable", False):
                    equity = get_account_equity_safe()
                    if not _equity_guard(equity, config._policy, "short_scan"):
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
                        log_event(f"SIZE {symbol}: ❌ sin tamaño ({sizing['reason']})")
                        continue
                    allowed, adj_notional, reason = _apply_event_and_cutoff_policies(
                        symbol, sizing["notional"], config._policy
                    )
                    if not allowed or adj_notional <= 0:
                        log_event(f"ENTRY {symbol}: ❌ veto por {reason}")
                        continue
                    if adj_notional != sizing["notional"]:
                        price = current_price
                        allow_frac = _cfg_risk(config._policy)["allow_fractional"]
                        if allow_frac:
                            new_shares = adj_notional / price
                        else:
                            new_shares = int(adj_notional // price)
                        if new_shares <= 0:
                            log_event(
                                f"ENTRY {symbol}: ❌ tamaño tras reducción no válido ({reason})"
                            )
                            continue
                        sizing["shares"] = new_shares
                        sizing["notional"] = new_shares * price
                        log_event(
                            f"ENTRY {symbol}: ⚠️ tamaño reducido por {reason} -> shares={new_shares:.4f} notional=${sizing['notional']:.2f}"
                        )
                    log_event(
                        f"SIZE {symbol}: ✅ shares={sizing['shares']:.4f} notional=${sizing['notional']:.2f} "
                        f"stop_dist=${sizing['stop_distance']:.4f} risk_budget=${sizing['risk_budget']:.2f} exposure={exposure:.2f}"
                    )
                    success = place_short_order_with_trailing_buy(symbol, sizing, 1.0)
                    if not success:
                        log_event(f"❌ Falló la orden short para {symbol}")
            except Exception as e:
                print(f"❌ Error verificando shortabilidad de {symbol}: {e}", flush=True)
        log_event(
            f"🔻 Total invertido en este ciclo de shorts: {invested_today_usd():.2f} USD",
        )
        time.sleep(300)


# ---------------------------------------------------------------------------
# Nuevo flujo de ejecución robusta
# ---------------------------------------------------------------------------


def _wait_for_fill_or_timeout(client_order_id: str, timeout_sec: int):
    import time

    start = time.time()
    delay = 0.5
    last_status = None
    while time.time() - start < timeout_sec:
        st = broker.get_order_status_by_client_id(client_order_id)
        last_status = st
        if st and st.state in ("filled", "partially_filled", "rejected", "canceled"):
            return st
        time.sleep(delay)
        delay = min(delay * 1.8, 2.0)
    return last_status or type("S", (), {"state": "timeout"})


def _on_fill_success(symbol, coid, status, cfg):
    filled_qty = getattr(status, "filled_qty", 0)
    avg_price = getattr(status, "filled_avg_price", 0)
    StateManager.add_executed_symbol(symbol)
    StateManager.add_open_position(symbol, coid, filled_qty, avg_price)
    executed_symbols_today.add(symbol)
    mark_executed(symbol)
    amount_usd = float(filled_qty) * float(avg_price)
    with pending_trades_lock:
        pending_trades.add(
            f"{symbol}: {filled_qty} unidades — ${amount_usd:.2f}"
        )
    log_event(f"FILL {symbol}: qty={filled_qty} avg={avg_price} coid={coid}")


def _reconcile_existing_order(symbol, coid, cfg):
    st = broker.get_order_status_by_client_id(coid)
    if not st:
        StateManager.remove_open_order(symbol, coid)
        return False
    if st.state in ("filled", "partially_filled"):
        _on_fill_success(symbol, coid, st, cfg)
        return True
    if st.state in ("new", "accepted", "open"):
        StateManager.add_open_order(symbol, coid)
        log_event(f"RECONCILE {symbol}: open order restored in StateManager")
        return True
    StateManager.remove_open_order(symbol, coid)
    log_event(f"RECONCILE {symbol}: state={st.state}, cleaned")
    return False


def _safe_reconcile_by_coid(symbol, coid, cfg):
    try:
        return _reconcile_existing_order(symbol, coid, cfg)
    except Exception as e:  # pragma: no cover - defensive
        log_event(f"RECONCILE {symbol}: error {e}")
        return False


def place_order_with_trailing_stop(
    symbol,
    side_or_sizing,
    shares: float | None = None,
    entry_type: str = "market",
    price_ctx: dict | None = None,
    cfg=None,
):
    """Place an order with idempotency, locking and reconciliation."""

    if isinstance(side_or_sizing, dict):
        # Backward compatibility with previous signature using sizing dict
        sizing = side_or_sizing
        side = "buy"
        shares = float(sizing.get("shares", 0))
    else:
        side = side_or_sizing
    lock = _get_symbol_lock(symbol)
    if not lock.acquire(blocking=False):
        log_event(f"ORDER {symbol}: lock busy, skipping")
        return False

    coid = make_client_order_id(symbol, side)
    try:
        if broker.order_exists(client_order_id=coid):
            log_event(
                f"ORDER {symbol}: already exists {coid}, reconciling instead of resending"
            )
            return _reconcile_existing_order(symbol, coid, cfg)

        StateManager.add_open_order(symbol, coid)

        ok, broker_order_id = broker.submit_order(
            symbol=symbol,
            side=side,
            qty=shares,
            client_order_id=coid,
            order_type=entry_type,
            price_ctx=price_ctx,
        )
        if not ok:
            StateManager.remove_open_order(symbol, coid)
            log_event(f"ORDER {symbol}: ❌ submit failed, cleaned open_orders")
            return False

        metrics.inc("ordered")

        status = _wait_for_fill_or_timeout(
            coid, timeout_sec=(cfg or {}).get("broker", {}).get("fill_timeout_sec", 20)
        )
        if status.state in ("filled", "partially_filled"):
            _on_fill_success(symbol, coid, status, cfg)
            StateManager.remove_open_order(symbol, coid)
            return True
        elif status.state in ("accepted", "new", "open"):
            log_event(f"ORDER {symbol}: pending after timeout -> monitoring")
            return True
        else:
            StateManager.remove_open_order(symbol, coid)
            log_event(
                f"ORDER {symbol}: ❌ state={getattr(status, 'state', None)}, removed from open_orders"
            )
            return False
    except Exception as e:
        log_event(f"ORDER {symbol}: ⛔ exception {e}")
        _safe_reconcile_by_coid(symbol, coid, cfg)
        return False
    finally:
        lock.release()
