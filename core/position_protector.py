"""Recurring position protection (break-even + trailing) for long positions."""

from __future__ import annotations

import math
import threading
import time
from typing import Optional

import yfinance as yf

import config
from broker import alpaca as broker
from core.order_protection import stop_limit_price
from core.safeguards import is_safeguards_active
from utils.logger import log_event

_PROTECT_LOCK = threading.Lock()
_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_ATR_CACHE: dict[str, tuple[float, float]] = {}
# Symbols whose shares are committed to an existing bracket stop.
# Suppressed for 30 min so we don't spam "insufficient qty" every tick.
_BRACKET_SUPPRESS: dict[str, float] = {}
_BRACKET_SUPPRESS_SEC = 1800

_PRICE_TTL_SEC = 15.0
_ATR_TTL_SEC = 300.0


def _risk_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("risk", {}) or {}


def _execution_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("execution", {}) or {}


def _safeguards_cfg() -> dict:
    return (getattr(config, "_policy", {}) or {}).get("safeguards", {}) or {}


def _price(symbol: str) -> Optional[float]:
    now = time.time()
    cached = _PRICE_CACHE.get(symbol)
    if cached and now - cached[0] < _PRICE_TTL_SEC:
        return cached[1]
    try:
        last = broker.get_current_price(symbol)
        if last and last > 0:
            val = float(last)
            _PRICE_CACHE[symbol] = (now, val)
            return val
    except Exception:
        return None
    return None


def _atr(symbol: str) -> Optional[float]:
    now = time.time()
    cached = _ATR_CACHE.get(symbol)
    if cached and now - cached[0] < _ATR_TTL_SEC:
        return cached[1]
    try:
        hist = yf.Ticker(symbol).history(period="3mo", interval="1d", timeout=3)
        if hist is None or hist.empty:
            return None
        high = hist["High"]
        low = hist["Low"]
        close = hist["Close"]
        prev_close = close.shift(1)
        tr = (high - low).combine((high - prev_close).abs(), max).combine((low - prev_close).abs(), max)
        atr_val = tr.rolling(window=14, min_periods=14).mean().iloc[-1]
        if atr_val is None:
            return None
        atr_float = float(atr_val)
        if atr_float <= 0:
            return None
        _ATR_CACHE[symbol] = (now, atr_float)
        return atr_float
    except Exception:
        return None


def _best_open_stop_for_symbol(open_orders, symbol: str) -> tuple[Optional[object], float]:
    best_order = None
    best_stop = 0.0
    for order in open_orders or []:
        try:
            if getattr(order, "symbol", "") != symbol:
                continue
            if str(getattr(order, "side", "")).lower() != "sell":
                continue
            order_type = str(getattr(order, "type", "") or getattr(order, "order_type", "")).lower()
            if order_type not in {"stop", "stop_limit"}:
                continue
            stop_raw = getattr(order, "stop_price", None)
            stop_price = float(stop_raw) if stop_raw is not None else 0.0
            if stop_price > best_stop:
                best_stop = stop_price
                best_order = order
        except Exception:
            continue
    return best_order, best_stop


def tick_protect_positions(*, dry_run: bool = False) -> None:
    """Run one non-blocking, idempotent protection cycle for open long positions."""

    if not _PROTECT_LOCK.acquire(blocking=False):
        log_event("skip reason=lock_busy", event="PROTECT")
        return

    try:
        safeguards_cfg = _safeguards_cfg()
        if not bool(safeguards_cfg.get("enabled", False)) or not is_safeguards_active():
            log_event("skip reason=safeguards_inactive", event="PROTECT")
            return

        risk_cfg = _risk_cfg()
        exec_cfg = _execution_cfg()
        break_even_r = float(safeguards_cfg.get("break_even_R", 1.0))
        break_even_buffer = float(safeguards_cfg.get("break_even_buffer_pct", 0.0))
        trailing_enable = bool(safeguards_cfg.get("trailing_enable", True))
        trailing_mult = float(exec_cfg.get("trailing_stop_atr_mult", 2.0))
        trailing_profit_mult = float(exec_cfg.get("trailing_stop_profit_atr_mult", 1.0))
        trailing_tighten_at_r = float(exec_cfg.get("trailing_tighten_at_R", 0.5))
        atr_k = float(risk_cfg.get("atr_k", 2.0))
        min_stop_pct = float(risk_cfg.get("min_stop_pct", 0.05))
        tick_ge_1 = float(risk_cfg.get("min_tick_equity_ge_1", 0.01))
        tick_lt_1 = float(risk_cfg.get("min_tick_equity_lt_1", 0.0001))
        protect_min_improve_pct = float(exec_cfg.get("protect_min_improvement_pct", 0.01))
        protect_min_improve_usd = float(exec_cfg.get("protect_min_improvement_usd", 0.10))
        # Protective stops must survive overnight; entry orders use "day"
        # because Alpaca market orders cannot be GTC.
        tif = exec_cfg.get("protect_time_in_force", "gtc")

        positions = broker.list_positions()
        try:
            open_orders = broker.api.list_orders(status="open", limit=500)
        except Exception:
            open_orders = []

        for pos in positions or []:
            try:
                symbol = str(getattr(pos, "symbol", "") or "").upper()
                qty = float(getattr(pos, "qty", 0) or 0)
                side = str(getattr(pos, "side", "") or "").lower()
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
                asset_class = str(getattr(pos, "asset_class", "us_equity") or "us_equity").lower()
            except Exception:
                continue
            if not symbol or qty <= 0 or entry <= 0:
                continue
            if side and side != "long":
                continue
            # Skip symbols whose shares are locked in a bracket stop order.
            if time.monotonic() < _BRACKET_SUPPRESS.get(symbol, 0):
                continue
            if asset_class not in {"us_equity", "equity"}:
                log_event(
                    f"symbol={symbol} asset_class={asset_class} reason=skip_non_equity",
                    event="PROTECT",
                )
                continue

            last = _price(symbol)
            atr = _atr(symbol)
            stop_order, old_stop = _best_open_stop_for_symbol(open_orders, symbol)
            old_stop = float(old_stop or 0.0)

            if not last or last <= 0:
                log_event(
                    f"symbol={symbol} entry={entry:.4f} last=0 atr={atr} old_stop={old_stop:.4f} new_stop={old_stop:.4f} reason=skip_no_price",
                    event="PROTECT",
                )
                continue

            tick = tick_ge_1 if last >= 1 else tick_lt_1
            initial_stop_distance = max((atr or 0.0) * atr_k, entry * min_stop_pct)
            initial_stop = entry - initial_stop_distance
            denom = max(entry - initial_stop, entry * min_stop_pct)
            r_multiple = (last - entry) / denom if denom > 0 else 0.0

            new_stop = old_stop
            reasons: list[str] = []

            if r_multiple >= break_even_r:
                be_stop = entry * (1 + break_even_buffer)
                if be_stop > new_stop + tick:
                    new_stop = be_stop
                    reasons.append("break_even")

            if trailing_enable:
                # Cuando la posición lleva >= trailing_tighten_at_r de ganancia,
                # apretamos el trailing para asegurar el beneficio acumulado.
                in_profit = r_multiple >= trailing_tighten_at_r
                effective_mult = trailing_profit_mult if in_profit else trailing_mult
                if atr and atr > 0:
                    trail_stop = last - atr * effective_mult
                else:
                    # Fallback sin ATR: 2% si en ganancia, 3% si todavía no
                    fallback_pct = 0.02 if in_profit else 0.03
                    trail_stop = last * (1 - fallback_pct)
                if trail_stop > new_stop + tick:
                    new_stop = trail_stop
                    reasons.append("trailing_profit" if in_profit else "trailing")

            # Only replace the order if the improvement is meaningful.
            # Three-way max: tick size floor, percentage of stop, and absolute
            # dollar floor.  The dollar floor prevents cheap stocks (e.g. TROX
            # at $6) from spamming updates because 0.5% of $6 is only $0.03 —
            # barely above the tick — while the trailing stop moves $0.03-$0.04
            # per minute.  With a $0.10 floor, TROX needs ~1h of price movement
            # before the stop is worth replacing.
            min_improve = (
                max(tick, old_stop * protect_min_improve_pct, protect_min_improve_usd)
                if old_stop > 0
                else tick
            )
            if new_stop <= old_stop + min_improve:
                log_event(
                    f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason=skip_no_improve",
                    event="PROTECT",
                )
                continue

            if new_stop >= last:
                log_event(
                    f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason=skip_invalid_stop",
                    event="PROTECT",
                )
                continue

            reason_txt = "+".join(reasons) if reasons else "update"
            if dry_run:
                log_event(
                    f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason={reason_txt} dry_run=1",
                    event="PROTECT",
                )
                continue

            try:
                if stop_order is not None and getattr(stop_order, "id", None):
                    broker.api.cancel_order(getattr(stop_order, "id"))
            except Exception as exc:
                log_event(
                    f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason=cancel_failed err={exc}",
                    event="PROTECT",
                )
                continue

            # Round to the valid tick increment before submission.
            # Alpaca enforces sub-penny rules: prices must be exact multiples of
            # $0.01 for equities >= $1.  Raw ATR arithmetic leaves fractional
            # cents that Alpaca rejects.  Round down so the stop is never tighter
            # than intended, then strip float-representation artifacts
            # (e.g. math.floor(x/0.01)*0.01 can yield 120.83000000000001).
            _decimals = 2 if new_stop >= 1.0 else 4
            price_tick = tick_ge_1 if new_stop >= 1.0 else tick_lt_1
            new_stop_clean = round(math.floor(new_stop / price_tick) * price_tick, _decimals)

            stop_payload = {"stop_price": new_stop_clean}
            limit = stop_limit_price(new_stop_clean, symbol=symbol)
            order_type = "stop"
            if limit and limit < new_stop_clean:
                _lim_dec = 2 if limit >= 1.0 else 4
                lim_tick = tick_ge_1 if limit >= 1.0 else tick_lt_1
                limit_clean = round(math.floor(limit / lim_tick) * lim_tick, _lim_dec)
                stop_payload["limit_price"] = limit_clean
                order_type = "stop_limit"

            # Include millisecond timestamp so each submission has a unique ID.
            # Using only the stop price caused "client_order_id must be unique"
            # errors when two ticks computed the same new_stop.
            client_order_id = f"PROTECT.{symbol}.{int(new_stop * 10000)}.{int(time.time() * 1000) % 1_000_000}"
            try:
                broker.api.submit_order(
                    symbol=symbol,
                    side="sell",
                    qty=qty,
                    type=order_type,
                    time_in_force=tif,
                    client_order_id=client_order_id,
                    **stop_payload,
                )
                log_event(
                    f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason={reason_txt}",
                    event="PROTECT",
                )
            except Exception as exc:
                err_str = str(exc)
                if "insufficient qty" in err_str:
                    # Shares are committed to an existing bracket stop order.
                    # Suppress this symbol for 30 min to avoid spam every tick.
                    _BRACKET_SUPPRESS[symbol] = time.monotonic() + _BRACKET_SUPPRESS_SEC
                    log_event(
                        f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason=protected_by_bracket suppress_min=30",
                        event="PROTECT",
                    )
                else:
                    log_event(
                        f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason=submit_failed err={exc}",
                        event="PROTECT",
                    )
    finally:
        _PROTECT_LOCK.release()
