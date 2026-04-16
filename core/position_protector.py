"""Recurring position protection (break-even + trailing) for long positions."""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
from typing import Optional

import yfinance as yf

import config
from broker import alpaca as broker
from core.order_protection import cancel_all_sells_and_wait, stop_limit_price
from core.safeguards import is_safeguards_active
from utils.logger import log_event
from utils.telegram_alert import send_telegram_alert

_PROTECT_LOCK = threading.Lock()
# File-level lock prevents multiple bot processes (e.g. two Render instances)
# from running the paper protect cycle simultaneously.
_PROTECT_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "paper_protect.lock"
)
_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_ATR_CACHE: dict[str, tuple[float, float]] = {}
# Symbols whose shares are committed to an existing bracket stop.
# Suppressed for 4 h (full trading session) so we don't spam "insufficient qty" every tick.
_BRACKET_SUPPRESS: dict[str, float] = {}
_BRACKET_SUPPRESS_SEC = 14400        # 4 h — safe when original stop still protects
_BRACKET_SUPPRESS_NO_STOP_SEC = 300  # 5 min — used when position has NO stop at all
# Symbols for which a blown-stop market-sell has already been submitted.
# Suppressed for 5 min to prevent double-selling before Alpaca updates positions.
_BLOWN_STOP_SUPPRESS: dict[str, float] = {}
_BLOWN_STOP_SUPPRESS_SEC = 300

# Stop high-water mark: highest stop successfully placed per symbol.
# Prevents re-placing a stop BELOW a level previously accepted (e.g. when a
# day-TIF bracket stop expires overnight and best_stop resets to 0).
_PAPER_STOP_HWM: dict[str, float] = {}
_PAPER_STOP_HWM_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "paper_stop_hwm.json"
)
_PAPER_STOP_HWM_LOADED = False

_PRICE_TTL_SEC = 15.0
_ATR_TTL_SEC = 300.0


def _save_paper_stop_hwm() -> None:
    """Persist paper stop HWM to disk."""
    try:
        os.makedirs(os.path.dirname(_PAPER_STOP_HWM_FILE), exist_ok=True)
        with open(_PAPER_STOP_HWM_FILE, "w") as fh:
            json.dump(dict(_PAPER_STOP_HWM), fh)
    except Exception:
        pass


def _load_paper_stop_hwm_once() -> None:
    """Load persisted paper stop HWM on first call."""
    global _PAPER_STOP_HWM_LOADED
    if _PAPER_STOP_HWM_LOADED:
        return
    _PAPER_STOP_HWM_LOADED = True
    try:
        with open(_PAPER_STOP_HWM_FILE) as fh:
            data = json.load(fh)
        for sym, val in data.items():
            _PAPER_STOP_HWM[sym] = float(val)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    except Exception:
        pass


def _update_paper_stop_hwm(symbol: str, stop: float) -> None:
    """Update paper HWM if stop is a new high; save to disk."""
    if stop > _PAPER_STOP_HWM.get(symbol, 0.0):
        _PAPER_STOP_HWM[symbol] = stop
        _save_paper_stop_hwm()


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
        # On network failure return the stale cached value rather than None so
        # the protection cycle can still act on the last known price.
        if cached:
            return cached[1]
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

    # Cross-process lock: prevents two bot instances from running the paper
    # protect cycle at the same time (e.g. two Render deployments).
    try:
        os.makedirs(os.path.dirname(_PROTECT_LOCK_PATH), exist_ok=True)
        _lock_fd = open(_PROTECT_LOCK_PATH, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _PROTECT_LOCK.release()
        log_event("skip reason=file_lock_busy", event="PROTECT")
        return

    try:
        _load_paper_stop_hwm_once()  # Load persisted stop HWM once per process lifetime.

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
        trailing_2r_mult = float(exec_cfg.get("trailing_stop_2r_atr_mult", 0.75))
        trailing_3r_mult = float(exec_cfg.get("trailing_stop_3r_atr_mult", 0.5))
        trailing_tighten_at_r = float(exec_cfg.get("trailing_tighten_at_R", 0.5))
        atr_k = float(risk_cfg.get("atr_k", 2.0))
        min_stop_pct = float(risk_cfg.get("min_stop_pct", 0.05))
        tick_ge_1 = float(risk_cfg.get("min_tick_equity_ge_1", 0.01))
        tick_lt_1 = float(risk_cfg.get("min_tick_equity_lt_1", 0.0001))
        protect_min_improve_pct = float(exec_cfg.get("protect_min_improvement_pct", 0.01))
        protect_min_improve_usd = float(exec_cfg.get("protect_min_improvement_usd", 0.10))
        min_profit_lock_pct = float(exec_cfg.get("min_profit_lock_pct", 0.0))
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
            # Skip symbols where a blown-stop market-sell was already submitted.
            # Prevents double-selling before Alpaca reflects the closed position.
            if time.monotonic() < _BLOWN_STOP_SUPPRESS.get(symbol, 0):
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

            # --- Blown stop detection ---
            # A stop-limit is "blown" when price has already gapped below the
            # stop level while the order is still open (limit not filled).
            # We only act when the gap exceeds blown_stop_gap_atr_multiplier × ATR
            # so that tiny sub-cent dips (which could self-correct) are ignored.
            # If ATR is unavailable we treat any gap as blown (fail-safe).
            blown_gap_mult = float(risk_cfg.get("blown_stop_gap_atr_multiplier", 0.0))
            if old_stop > 0 and last < old_stop and stop_order is not None:
                order_type_str = str(
                    getattr(stop_order, "type", "") or getattr(stop_order, "order_type", "")
                ).lower()
                if order_type_str == "stop_limit":
                    gap = old_stop - last
                    atr_threshold = (atr or 0.0) * blown_gap_mult
                    if blown_gap_mult > 0 and atr_threshold > 0 and gap < atr_threshold:
                        # Gap is smaller than threshold — stop-limit may still fill
                        # on a price bounce; skip and revisit next cycle.
                        log_event(
                            f"symbol={symbol} entry={entry:.4f} last={last:.4f} "
                            f"old_stop={old_stop:.4f} gap={gap:.4f} atr={float(atr or 0):.4f} "
                            f"threshold={atr_threshold:.4f} reason=blown_stop_gap_too_small",
                            event="PROTECT",
                        )
                        continue
                    log_event(
                        f"symbol={symbol} entry={entry:.4f} last={last:.4f} "
                        f"old_stop={old_stop:.4f} gap={gap:.4f} atr={float(atr or 0):.4f} "
                        f"reason=blown_stop_detected",
                        event="PROTECT",
                    )
                    # Guard against double-sell after process restart: check Alpaca for
                    # any pending market sell order (in-memory suppress resets on restart).
                    _pending_sell = any(
                        getattr(o, "symbol", "") == symbol
                        and str(getattr(o, "side", "")).lower() == "sell"
                        and str(
                            getattr(o, "type", "") or getattr(o, "order_type", "")
                        ).lower() == "market"
                        for o in (open_orders or [])
                    )
                    if _pending_sell:
                        _BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _BLOWN_STOP_SUPPRESS_SEC
                        log_event(
                            f"symbol={symbol} entry={entry:.4f} last={last:.4f} "
                            f"old_stop={old_stop:.4f} reason=blown_stop_already_pending",
                            event="PROTECT",
                        )
                        continue
                    if not dry_run:
                        # Cancel ALL open sell orders and wait for Alpaca to confirm
                        # before submitting market close (avoids "insufficient qty").
                        _cleared = cancel_all_sells_and_wait(broker.api, symbol, open_orders)
                        if not _cleared:
                            log_event(
                                f"symbol={symbol} reason=blown_stop_cancel_wait_failed",
                                event="PROTECT",
                            )
                            send_telegram_alert(
                                f"⚠️ PAPER {symbol}: cancel_wait_timed_out (blown_stop) — sell orders still open after retries. Suppressing."
                            )
                            # Do NOT attempt market-sell while shares are still locked
                            # in Alpaca sell orders — it will fail with "insufficient qty".
                            _BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _BLOWN_STOP_SUPPRESS_SEC
                        else:
                            # A TP limit may have partially or fully filled during the
                            # 800 ms wait. Fetch real position qty before market-selling.
                            _sell_qty = qty
                            try:
                                _sell_qty = float(getattr(broker.api.get_position(symbol), "qty", qty))
                            except Exception:
                                pass
                            if _sell_qty <= 0:
                                log_event(
                                    f"symbol={symbol} reason=blown_stop_position_already_closed",
                                    event="PROTECT",
                                )
                            else:
                                if _sell_qty != qty:
                                    log_event(
                                        f"symbol={symbol} qty_adjusted orig={qty:.0f} real={_sell_qty:.0f} reason=partial_tp_fill",
                                        event="PROTECT",
                                    )
                                    send_telegram_alert(
                                        f"ℹ️ PAPER {symbol}: blown_stop qty adjusted {qty:.0f}→{_sell_qty:.0f} (partial TP fill during cancel window)"
                                    )
                                try:
                                    client_order_id = f"BLOWNSTOP.{symbol}.{int(time.time() * 1000) % 1_000_000}"
                                    broker.api.submit_order(
                                        symbol=symbol,
                                        side="sell",
                                        qty=_sell_qty,
                                        type="market",
                                        time_in_force="day",
                                        client_order_id=client_order_id,
                                    )
                                    _BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _BLOWN_STOP_SUPPRESS_SEC
                                    log_event(
                                        f"symbol={symbol} entry={entry:.4f} last={last:.4f} "
                                        f"old_stop={old_stop:.4f} qty={_sell_qty:.0f} reason=blown_stop_market_sell",
                                        event="PROTECT",
                                    )
                                except Exception as exc:
                                    log_event(
                                        f"symbol={symbol} reason=blown_stop_market_sell_failed err={exc}",
                                        event="PROTECT",
                                    )
                                    send_telegram_alert(
                                        f"🚨 PAPER {symbol}: blown stop — market sell FAILED ({exc}). Position still open below stop at {last:.2f}! Manual close required."
                                    )
                    continue

            tick = tick_ge_1 if last >= 1 else tick_lt_1
            initial_stop_distance = max((atr or 0.0) * atr_k, entry * min_stop_pct)
            initial_stop = entry - initial_stop_distance
            denom = max(entry - initial_stop, entry * min_stop_pct)
            r_multiple = (last - entry) / denom if denom > 0 else 0.0

            # Never allow the stop to fall below: (1) initial ATR floor,
            # (2) high-water mark — the highest stop ever successfully set.
            # The HWM ensures that even when a day-TIF bracket stop expires
            # overnight (old_stop resets to 0), the replacement is never placed
            # below a level that was already earned via trailing.
            hwm = _PAPER_STOP_HWM.get(symbol, 0.0)
            new_stop = max(old_stop, initial_stop, hwm)
            reasons: list[str] = []

            if r_multiple >= break_even_r:
                be_stop = entry * (1 + break_even_buffer)
                if be_stop > new_stop + tick:
                    new_stop = be_stop
                    reasons.append("break_even")

            # ATR-independent profit lock: once price is up >= min_profit_lock_pct %
            # from entry, force stop to at least break-even (entry + buffer).
            if min_profit_lock_pct > 0 and entry > 0:
                gain_pct = (last - entry) / entry * 100
                if gain_pct >= min_profit_lock_pct:
                    lock_stop = entry * (1 + break_even_buffer)
                    if lock_stop > new_stop + tick:
                        new_stop = lock_stop
                        reasons.append("profit_lock")

            if trailing_enable:
                # Multi-tier trailing: progressively tighter as R-multiple grows.
                # The more we've earned, the closer the stop follows the price.
                # Tier 0 (<0.2R):  1.5×ATR — room to breathe before profit
                # Tier 1 (0.2–2R): 1.0×ATR — standard profit trail
                # Tier 2 (2–3R):   0.75×ATR — capture more of mid-run gains
                # Tier 3 (3R+):    0.5×ATR  — lock 90%+ of big winners
                if r_multiple >= 3.0:
                    effective_mult = trailing_3r_mult
                    trail_label = "trailing_3r"
                elif r_multiple >= 2.0:
                    effective_mult = trailing_2r_mult
                    trail_label = "trailing_2r"
                elif r_multiple >= trailing_tighten_at_r:
                    effective_mult = trailing_profit_mult
                    trail_label = "trailing_profit"
                else:
                    effective_mult = trailing_mult
                    trail_label = "trailing"
                if atr and atr > 0:
                    trail_stop = last - atr * effective_mult
                else:
                    # Fallback sin ATR: escala con R-multiple
                    if r_multiple >= 3.0:
                        fallback_pct = 0.01
                    elif r_multiple >= 2.0:
                        fallback_pct = 0.015
                    elif r_multiple >= trailing_tighten_at_r:
                        fallback_pct = 0.02
                    else:
                        fallback_pct = 0.03
                    trail_stop = last * (1 - fallback_pct)
                if trail_stop > new_stop + tick:
                    new_stop = trail_stop
                    reasons.append(trail_label)

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
                # If there is no existing stop order and price has already fallen
                # below the intended stop level, close immediately with a market
                # sell rather than silently leaving the position unprotected.
                if old_stop == 0:
                    _pending_sell = any(
                        getattr(o, "symbol", "") == symbol
                        and str(getattr(o, "side", "")).lower() == "sell"
                        for o in (open_orders or [])
                    )
                    if _pending_sell:
                        _BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _BLOWN_STOP_SUPPRESS_SEC
                        log_event(
                            f"symbol={symbol} entry={entry:.4f} last={last:.4f} "
                            f"new_stop={new_stop:.4f} reason=no_stop_below_stop_already_pending",
                            event="PROTECT",
                        )
                    elif not dry_run:
                        # Cancel ALL open sell orders and wait for Alpaca to confirm
                        # before submitting market close (avoids "insufficient qty").
                        _cleared = cancel_all_sells_and_wait(broker.api, symbol, open_orders)
                        if not _cleared:
                            log_event(
                                f"symbol={symbol} reason=no_stop_cancel_wait_failed",
                                event="PROTECT",
                            )
                            send_telegram_alert(
                                f"⚠️ PAPER {symbol}: cancel_wait_timed_out (no_stop) — sell orders still open after retries. Suppressing."
                            )
                            # Do NOT attempt market-sell while shares are still locked
                            # in Alpaca sell orders — it will fail with "insufficient qty".
                            _BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _BLOWN_STOP_SUPPRESS_SEC
                        else:
                            # A TP limit may have partially or fully filled during the
                            # 800 ms wait. Fetch real position qty before market-selling.
                            _sell_qty = qty
                            try:
                                _sell_qty = float(getattr(broker.api.get_position(symbol), "qty", qty))
                            except Exception:
                                pass
                            if _sell_qty <= 0:
                                log_event(
                                    f"symbol={symbol} reason=no_stop_position_already_closed",
                                    event="PROTECT",
                                )
                            else:
                                if _sell_qty != qty:
                                    log_event(
                                        f"symbol={symbol} qty_adjusted orig={qty:.0f} real={_sell_qty:.0f} reason=partial_tp_fill",
                                        event="PROTECT",
                                    )
                                    send_telegram_alert(
                                        f"ℹ️ PAPER {symbol}: no_stop qty adjusted {qty:.0f}→{_sell_qty:.0f} (partial TP fill during cancel window)"
                                    )
                                try:
                                    client_order_id = f"NOSTOP.{symbol}.{int(time.time() * 1000) % 1_000_000}"
                                    broker.api.submit_order(
                                        symbol=symbol,
                                        side="sell",
                                        qty=_sell_qty,
                                        type="market",
                                        time_in_force="day",
                                        client_order_id=client_order_id,
                                    )
                                    _BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _BLOWN_STOP_SUPPRESS_SEC
                                    log_event(
                                        f"symbol={symbol} entry={entry:.4f} last={last:.4f} "
                                        f"new_stop={new_stop:.4f} qty={_sell_qty:.0f} reason=no_stop_price_below_stop_market_sell",
                                        event="PROTECT",
                                    )
                                except Exception as exc:
                                    log_event(
                                        f"symbol={symbol} reason=no_stop_market_sell_failed err={exc}",
                                        event="PROTECT",
                                    )
                                    send_telegram_alert(
                                        f"🚨 PAPER {symbol}: no stop + price at/below stop level — market sell FAILED ({exc}). Position unprotected at {last:.2f}! Manual close required."
                                    )
                    else:
                        log_event(
                            f"symbol={symbol} entry={entry:.4f} last={last:.4f} "
                            f"new_stop={new_stop:.4f} reason=no_stop_price_below_stop dry_run=1",
                            event="PROTECT",
                        )
                else:
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
                _update_paper_stop_hwm(symbol, new_stop)
                log_event(
                    f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason={reason_txt}",
                    event="PROTECT",
                )
            except Exception as exc:
                err_str = str(exc)
                if "insufficient qty" in err_str:
                    # One or more sell orders (TP limit, stops) are tying up all
                    # shares. Cancel ALL of them, wait for Alpaca to confirm, then
                    # retry; the TP renewal pass re-places take-profits later.
                    _has_sells = any(
                        getattr(o, "symbol", "") == symbol
                        and str(getattr(o, "side", "")).lower() == "sell"
                        for o in (open_orders or [])
                    )
                    if _has_sells:
                        _cleared = cancel_all_sells_and_wait(broker.api, symbol, open_orders)
                        if _cleared:
                            try:
                                _retry_id = f"PROTECT.{symbol}.{int(new_stop * 10000)}.{int(time.time() * 1000) % 1_000_000}"
                                broker.api.submit_order(
                                    symbol=symbol,
                                    side="sell",
                                    qty=qty,
                                    type=order_type,
                                    time_in_force=tif,
                                    client_order_id=_retry_id,
                                    **stop_payload,
                                )
                                _update_paper_stop_hwm(symbol, new_stop)
                                log_event(
                                    f"symbol={symbol} entry={entry:.4f} last={last:.4f} "
                                    f"new_stop={new_stop:.4f} reason={reason_txt}_cancel_tp_placed_stop",
                                    event="PROTECT",
                                )
                            except Exception as exc2:
                                log_event(
                                    f"symbol={symbol} stop_after_cancel_tp_failed err={exc2}",
                                    event="PROTECT",
                                )
                                send_telegram_alert(
                                    f"🚨 PAPER {symbol}: cancelled all sells but stop still failed ({exc2}) — position has NO stop and NO TP!"
                                )
                                # Position unprotected: retry in 5 min, not 4 h.
                                _BRACKET_SUPPRESS[symbol] = time.monotonic() + _BRACKET_SUPPRESS_NO_STOP_SEC
                        else:
                            log_event(
                                f"symbol={symbol} cancel_wait_timed_out reason=stop_suppressed",
                                event="PROTECT",
                            )
                            send_telegram_alert(
                                f"⚠️ PAPER {symbol}: cancel_wait_timed_out (stop placement) — blocking sell orders not cleared. Stop suppressed."
                            )
                            # Blocking sells still open: retry in 5 min.
                            _BRACKET_SUPPRESS[symbol] = time.monotonic() + _BRACKET_SUPPRESS_NO_STOP_SEC
                    else:
                        _BRACKET_SUPPRESS[symbol] = time.monotonic() + _BRACKET_SUPPRESS_SEC
                        log_event(
                            f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason=protected_by_bracket suppress_min=240",
                            event="PROTECT",
                        )
                else:
                    log_event(
                        f"symbol={symbol} entry={entry:.4f} last={last:.4f} atr={float(atr or 0):.4f} old_stop={old_stop:.4f} new_stop={new_stop:.4f} reason=submit_failed err={exc}",
                        event="PROTECT",
                    )

        # --- Take-profit renewal pass ---
        # Bracket TP legs (limit sells) use day TIF and expire at EOD.
        # Alpaca also cancels the TP leg when replace_order() updates the stop.
        # Re-place a standalone GTC limit-sell whenever no open TP exists.
        tp_mult = float(exec_cfg.get("take_profit_atr_mult", 3.0))
        for pos in positions or []:
            try:
                symbol = str(getattr(pos, "symbol", "") or "").upper()
                qty = float(getattr(pos, "qty", 0) or 0)
                side = str(getattr(pos, "side", "") or "").lower()
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
                asset_class = str(getattr(pos, "asset_class", "us_equity") or "us_equity").lower()
            except Exception:
                continue
            if not symbol or qty <= 0 or entry <= 0 or (side and side != "long"):
                continue
            if asset_class not in {"us_equity", "equity"}:
                continue
            if time.monotonic() < _BLOWN_STOP_SUPPRESS.get(symbol, 0):
                continue
            # Skip if shares are committed to a bracket/stop order.
            if time.monotonic() < _BRACKET_SUPPRESS.get(symbol, 0):
                continue

            has_tp = any(
                getattr(o, "symbol", "") == symbol
                and str(getattr(o, "side", "")).lower() == "sell"
                and str(getattr(o, "type", "") or getattr(o, "order_type", "")).lower() == "limit"
                for o in (open_orders or [])
            )
            if has_tp:
                continue

            # If stop/stop_limit orders already commit all shares, Alpaca will
            # reject a standalone limit-sell with "insufficient qty available".
            # Skip the TP attempt entirely — no API call, no error, no suppress needed.
            stop_committed_qty = sum(
                float(getattr(o, "qty", 0) or 0)
                for o in (open_orders or [])
                if getattr(o, "symbol", "") == symbol
                and str(getattr(o, "side", "")).lower() == "sell"
                and str(
                    getattr(o, "type", "") or getattr(o, "order_type", "")
                ).lower() in {"stop", "stop_limit"}
            )
            if stop_committed_qty >= qty:
                continue

            atr_tp = _atr(symbol)
            if not atr_tp or atr_tp <= 0:
                continue
            last_tp = _price(symbol)
            if not last_tp or last_tp <= 0:
                continue

            computed_tp = round(entry + atr_tp * tp_mult, 2)
            if computed_tp <= last_tp:
                log_event(
                    f"symbol={symbol} entry={entry:.4f} computed_tp={computed_tp:.2f} "
                    f"last={last_tp:.4f} reason=tp_skip_price_above_target",
                    event="PROTECT",
                )
                continue

            tif_tp = exec_cfg.get("protect_time_in_force", "gtc")
            if dry_run:
                log_event(
                    f"symbol={symbol} entry={entry:.4f} computed_tp={computed_tp:.2f} "
                    f"reason=tp_renewal dry_run=1",
                    event="PROTECT",
                )
                continue

            try:
                client_order_id = f"PROTECT.TP.{symbol}.{int(computed_tp * 100)}.{int(time.time() * 1000) % 1_000_000}"
                broker.api.submit_order(
                    symbol=symbol,
                    side="sell",
                    qty=qty,
                    type="limit",
                    time_in_force=tif_tp,
                    limit_price=computed_tp,
                    client_order_id=client_order_id,
                )
                log_event(
                    f"symbol={symbol} entry={entry:.4f} last={last_tp:.4f} "
                    f"atr={atr_tp:.4f} tp={computed_tp:.2f} reason=tp_renewal",
                    event="PROTECT",
                )
            except Exception as exc:
                err_str = str(exc)
                if "insufficient qty" in err_str:
                    _BRACKET_SUPPRESS[symbol] = time.monotonic() + _BRACKET_SUPPRESS_SEC
                log_event(
                    f"symbol={symbol} tp_submit_failed err={exc}",
                    event="PROTECT",
                )

    finally:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except Exception:
            pass
        _PROTECT_LOCK.release()
