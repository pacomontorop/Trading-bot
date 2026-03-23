"""Order placement and position protection for the live (real-money) Alpaca account.

``place_live_order`` submits a bracket buy via the live API.
``tick_protect_live_positions`` runs break-even and trailing-stop upgrades
for all open live long positions (same logic as the paper protector).
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
from typing import Optional

import config
from broker.alpaca import get_current_price
from broker.alpaca_live import live_api, list_live_positions, list_live_open_orders
from core.broker import get_tick_size, round_to_tick
from core.market_gate import is_us_equity_market_open
from core.order_protection import cancel_all_sells_and_wait, compute_bracket_prices, stop_limit_price, validate_bracket_prices
from core.safeguards import is_safeguards_active
from utils.logger import log_event
from utils.telegram_alert import send_telegram_alert

_LIVE_PROTECT_LOCK = threading.Lock()
_ATR_CACHE: dict[str, tuple[float, float]] = {}
_ATR_TTL_SEC = 300.0
# Symbols for which a blown-stop market-sell has already been submitted.
# Suppressed for 5 min to prevent double-selling before Alpaca updates positions.
_LIVE_BLOWN_STOP_SUPPRESS: dict[str, float] = {}
_LIVE_BLOWN_STOP_SUPPRESS_SEC = 300
# Symbols where Alpaca reported "insufficient qty available" (all qty in bracket).
# 4-hour suppress is safe when the *original* stop order is still active —
# we're merely postponing the trailing improvement.
_LIVE_INSUF_QTY_SUPPRESS: dict[str, float] = {}
_LIVE_INSUF_QTY_SUPPRESS_SEC = 14400  # 4 hours — original stop still protects

# Shorter retry window used when a position has NO stop at all (unprotected).
# 5 minutes: fast enough to re-establish protection without hammering the API.
_LIVE_NO_STOP_RETRY_SEC = 300  # 5 minutes

# Per-symbol Telegram alert cooldown for repeated stop-failure noise suppression.
# Prevents a Telegram flood while we keep retrying every 60-second tick.
_LIVE_STOP_ALERT_COOLDOWN: dict[str, float] = {}
_LIVE_STOP_ALERT_COOLDOWN_SEC = 300  # 5 minutes between repeated alerts

# File-based lock path for cross-process protection dedup (multiple Render instances).
_LIVE_PROTECT_FLOCK_PATH = "/tmp/live_protect_cycle.lock"

# Tracks the (old_stop, new_stop) from a replace_order call that appeared to
# succeed at the API level.  On the next cycle we check whether the stop price
# actually changed; if not, Alpaca rejected the replacement asynchronously and
# we suppress retries for _LIVE_NO_STOP_RETRY_SEC.
_LIVE_LAST_REPLACE: dict[str, tuple[float, float]] = {}  # symbol -> (old, expected_new)

# Stop high-water mark: highest stop successfully placed per symbol.
# When best_stop == 0 (stop expired/cancelled), the HWM is used as a floor so
# we never re-place a stop BELOW a level that was previously accepted.
# Stops only ever move up — this enforces that invariant across session breaks.
_LIVE_STOP_HWM: dict[str, float] = {}
_LIVE_STOP_HWM_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "live_stop_hwm.json"
)
_LIVE_STOP_HWM_LOADED = False


def _save_stop_hwm() -> None:
    """Persist stop HWM entries to disk."""
    try:
        os.makedirs(os.path.dirname(_LIVE_STOP_HWM_FILE), exist_ok=True)
        with open(_LIVE_STOP_HWM_FILE, "w") as fh:
            json.dump(dict(_LIVE_STOP_HWM), fh)
    except Exception:
        pass


def _load_stop_hwm_once() -> None:
    """Load persisted stop HWM on first call (survives restarts)."""
    global _LIVE_STOP_HWM_LOADED
    if _LIVE_STOP_HWM_LOADED:
        return
    _LIVE_STOP_HWM_LOADED = True
    try:
        with open(_LIVE_STOP_HWM_FILE) as fh:
            data = json.load(fh)
        for sym, val in data.items():
            _LIVE_STOP_HWM[sym] = float(val)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    except Exception:
        pass


def _update_stop_hwm(symbol: str, stop: float) -> None:
    """Update HWM if stop is a new high; save to disk."""
    if stop > _LIVE_STOP_HWM.get(symbol, 0.0):
        _LIVE_STOP_HWM[symbol] = stop
        _save_stop_hwm()

# Path for persisting _LIVE_INSUF_QTY_SUPPRESS across restarts.
_LIVE_SUPPRESS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "live_suppress.json"
)
_LIVE_SUPPRESS_LOADED = False


def _save_suppress() -> None:
    """Persist current suppress entries to disk (wall-clock expiry times)."""
    now_mono = time.monotonic()
    now_wall = time.time()
    data = {
        sym: now_wall + (exp_mono - now_mono)
        for sym, exp_mono in _LIVE_INSUF_QTY_SUPPRESS.items()
        if exp_mono > now_mono
    }
    try:
        os.makedirs(os.path.dirname(_LIVE_SUPPRESS_FILE), exist_ok=True)
        with open(_LIVE_SUPPRESS_FILE, "w") as fh:
            json.dump(data, fh)
    except Exception:
        pass


def _load_suppress_once() -> None:
    """Load persisted suppresses on first call (survives restarts)."""
    global _LIVE_SUPPRESS_LOADED
    if _LIVE_SUPPRESS_LOADED:
        return
    _LIVE_SUPPRESS_LOADED = True
    try:
        with open(_LIVE_SUPPRESS_FILE) as fh:
            data = json.load(fh)
        now_wall = time.time()
        now_mono = time.monotonic()
        for sym, exp_wall in data.items():
            remaining = exp_wall - now_wall
            if remaining > 0:
                _LIVE_INSUF_QTY_SUPPRESS[sym] = now_mono + remaining
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    except Exception:
        pass

# Crypto symbols (e.g. BTCUSD, ETHUSD) are not managed by this bot's equity logic.
# Alpaca crypto tickers typically end in USD with length >= 6, or contain "/".
_CRYPTO_SUFFIXES = {"USD", "BTC", "ETH", "USDT", "USDC"}


def _is_crypto_symbol(symbol: str) -> bool:
    if "/" in symbol:
        return True
    for suffix in _CRYPTO_SUFFIXES:
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return True
    return False


def _atr(symbol: str) -> Optional[float]:
    """Fetch ATR for ``symbol`` with a 5-minute cache."""
    now = time.time()
    cached = _ATR_CACHE.get(symbol)
    if cached and now - cached[0] < _ATR_TTL_SEC:
        return cached[1]
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period="3mo", interval="1d", timeout=3)
        if hist is None or hist.empty:
            return None
        high = hist["High"]
        low = hist["Low"]
        close = hist["Close"]
        prev_close = close.shift(1)
        tr = (
            (high - low)
            .combine((high - prev_close).abs(), max)
            .combine((low - prev_close).abs(), max)
        )
        atr_val = tr.rolling(window=14, min_periods=14).mean().iloc[-1]
        if atr_val is None or float(atr_val) <= 0:
            return None
        atr_float = float(atr_val)
        _ATR_CACHE[symbol] = (now, atr_float)
        return atr_float
    except Exception:
        return None


def place_live_order(plan: dict, *, dry_run: bool = False) -> bool:
    """Place a bracket buy order on the live Alpaca account.

    Returns ``True`` on success (or dry-run), ``False`` on failure.
    """
    symbol = plan.get("symbol")
    # Alpaca live rejects bracket orders with fractional qty.
    # Floor to integer; reject if the result is zero.
    qty = math.floor(float(plan.get("qty") or 0))
    price = float(plan.get("price") or 0)
    atr = plan.get("atr")
    time_in_force = plan.get("time_in_force", "day")

    if qty < 1 or not symbol:
        log_event(f"LIVE ORDER {symbol}: rejected reason=zero_qty", event="LIVE")
        return False

    if not is_safeguards_active():
        log_event(f"LIVE ORDER {symbol}: rejected reason=safeguards_inactive", event="LIVE")
        return False

    risk_cfg = (getattr(config, "_policy", {}) or {}).get("risk", {})
    exec_cfg = (getattr(config, "_policy", {}) or {}).get("execution", {})

    client_order_id = f"LIVE.LONG.{symbol}.{int(price * 100)}"

    if dry_run:
        log_event(
            f"LIVE DRY_RUN ORDER {symbol}: qty={qty:.0f} price={price:.2f} "
            f"stop={plan.get('stop_loss', 0):.2f} tp={plan.get('take_profit', 0):.2f}",
            event="LIVE",
        )
        return True

    bracket = compute_bracket_prices(
        symbol=symbol,
        entry_price=price,
        atr=atr,
        risk_cfg=risk_cfg,
        exec_cfg=exec_cfg,
    )
    stop_loss = bracket["stop_price"]
    take_profit = bracket["take_profit"]

    if not validate_bracket_prices(price, stop_loss, take_profit):
        log_event(f"LIVE ORDER {symbol}: rejected reason=invalid_bracket_prices", event="LIVE")
        return False

    stop_payload = {"stop_price": stop_loss}
    stop_limit = stop_limit_price(stop_loss, symbol=symbol)
    if stop_limit and stop_limit < stop_loss:
        stop_payload["limit_price"] = stop_limit

    log_event(
        f"LIVE ORDER_SUBMIT symbol={symbol} side=buy qty={qty:.0f} order_class=bracket "
        f"entry={price:.2f} atr={float(atr or 0):.4f} sl={stop_loss:.4f} tp={take_profit:.4f}",
        event="LIVE",
    )
    try:  # pragma: no cover - network
        live_api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="market",
            time_in_force=time_in_force,
            order_class="bracket",
            take_profit={"limit_price": take_profit},
            stop_loss=stop_payload,
            client_order_id=client_order_id,
        )
        return True
    except Exception as exc:  # pragma: no cover
        log_event(f"LIVE ORDER {symbol}: failed err={exc}", event="LIVE")
        return False


def tick_protect_live_positions(*, dry_run: bool = False) -> None:
    """Run one protection cycle for open live long positions.

    Upgrades stop-loss orders to break-even or trailing levels when the
    position has moved sufficiently in our favour.  Uses the same logic
    as ``core.position_protector.tick_protect_positions`` but operates
    entirely on the live Alpaca account.
    """
    if not _LIVE_PROTECT_LOCK.acquire(blocking=False):
        log_event("LIVE_PROTECT skip reason=lock_busy", event="LIVE")
        return

    # Cross-process lock: prevent duplicate protection cycles when Render runs
    # multiple instances simultaneously (e.g. during rolling deploys).
    _flock_fd = None
    try:
        _flock_fd = open(_LIVE_PROTECT_FLOCK_PATH, "w")
        fcntl.flock(_flock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        if _flock_fd is not None:
            _flock_fd.close()
        _LIVE_PROTECT_LOCK.release()
        log_event("LIVE_PROTECT skip reason=flock_busy", event="LIVE")
        return

    try:
        _load_suppress_once()   # Load persisted suppresses once per process lifetime.
        _load_stop_hwm_once()   # Load persisted stop HWM once per process lifetime.

        safeguards_cfg = (getattr(config, "_policy", {}) or {}).get("safeguards", {}) or {}
        if not bool(safeguards_cfg.get("enabled", False)) or not is_safeguards_active():
            log_event("LIVE_PROTECT skip reason=safeguards_inactive", event="LIVE")
            return

        risk_cfg = (getattr(config, "_policy", {}) or {}).get("risk", {}) or {}
        exec_cfg = (getattr(config, "_policy", {}) or {}).get("execution", {}) or {}

        break_even_r = float(safeguards_cfg.get("break_even_R", 1.0))
        break_even_buffer = float(safeguards_cfg.get("break_even_buffer_pct", 0.0))
        trailing_enable = bool(safeguards_cfg.get("trailing_enable", True))
        trailing_mult = float(exec_cfg.get("trailing_stop_atr_mult", 1.5))
        trailing_profit_mult = float(exec_cfg.get("trailing_stop_profit_atr_mult", 1.5))
        trailing_tighten_at_r = float(exec_cfg.get("trailing_tighten_at_R", 0.3))
        min_profit_lock_pct = float(exec_cfg.get("min_profit_lock_pct", 0.0))
        atr_k = float(risk_cfg.get("atr_k", 2.0))
        min_stop_pct = float(risk_cfg.get("min_stop_pct", 0.05))
        tick_ge_1 = float(risk_cfg.get("min_tick_equity_ge_1", 0.01))
        tick_lt_1 = float(risk_cfg.get("min_tick_equity_lt_1", 0.0001))
        tif = exec_cfg.get("protect_time_in_force", "gtc")

        positions = list_live_positions()
        open_orders = list_live_open_orders()

        for pos in positions or []:
            try:
                symbol = str(getattr(pos, "symbol", "") or "").upper()
                qty = float(getattr(pos, "qty", 0) or 0)
                side = str(getattr(pos, "side", "") or "").lower()
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            except Exception:
                continue
            if not symbol or qty <= 0 or entry <= 0:
                continue
            if side and side != "long":
                continue
            # Skip symbols where a blown-stop market-sell was already submitted.
            if time.monotonic() < _LIVE_BLOWN_STOP_SUPPRESS.get(symbol, 0):
                continue
            if _is_crypto_symbol(symbol):
                log_event(
                    f"LIVE_PROTECT symbol={symbol} reason=skip_crypto",
                    event="LIVE",
                )
                continue
            # Skip symbols where qty was fully committed to a bracket order.
            if time.monotonic() < _LIVE_INSUF_QTY_SUPPRESS.get(symbol, 0):
                continue

            last = get_current_price(symbol)
            atr_val = _atr(symbol)

            # Find best existing stop order for this symbol
            best_order = None
            best_stop = 0.0
            for order in open_orders or []:
                try:
                    if getattr(order, "symbol", "") != symbol:
                        continue
                    if str(getattr(order, "side", "")).lower() != "sell":
                        continue
                    order_type = str(
                        getattr(order, "type", "") or getattr(order, "order_type", "")
                    ).lower()
                    if order_type not in {"stop", "stop_limit"}:
                        continue
                    stop_raw = getattr(order, "stop_price", None)
                    sp = float(stop_raw) if stop_raw is not None else 0.0
                    if sp > best_stop:
                        best_stop = sp
                        best_order = order
                except Exception:
                    continue

            # --- Async-rejection detection ---
            # replace_order() returns HTTP 200 but Alpaca can asynchronously
            # reject the replacement (visible as "rejected" in the orders UI).
            # The original stop is then cancelled, or the replacement silently
            # fails, leaving old_stop unchanged.  Detect this by comparing
            # best_stop with what we expected after the last replace attempt.
            if symbol in _LIVE_LAST_REPLACE:
                _last_old, _last_expected = _LIVE_LAST_REPLACE.pop(symbol)
                _tick_chk = get_tick_size(symbol, "us_equity", last or entry)
                # Compare actual stop against the *expected* new stop (not old+tick).
                # Using old+tick would cause a false positive when the improvement was
                # exactly 1 tick (new == old + tick satisfies the old check even though
                # the replacement succeeded).
                if best_stop < _last_expected - _tick_chk:
                    # Stop is meaningfully below what we expected → async rejection.
                    log_event(
                        f"LIVE_PROTECT symbol={symbol} replace_async_rejected "
                        f"expected={_last_expected:.4f} actual={best_stop:.4f} "
                        f"suppressing={_LIVE_NO_STOP_RETRY_SEC}s",
                        event="LIVE",
                    )
                    send_telegram_alert(
                        f"⚠️ LIVE {symbol}: replace_order async rejected (expected stop "
                        f"{_last_expected:.4f}, still at {best_stop:.4f}). "
                        f"Suppressing for 5 min."
                    )
                    _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                        time.monotonic() + _LIVE_NO_STOP_RETRY_SEC
                    )
                    _save_suppress()
                    continue

            if not last or last <= 0:
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last=0 reason=skip_no_price",
                    event="LIVE",
                )
                continue

            # --- Blown stop detection ---
            # A stop-limit is "blown" when price has gapped below the stop level
            # while the order is still open (limit never filled).
            # Only act when gap > blown_stop_gap_atr_multiplier × ATR so small
            # dips that may self-correct are not acted on prematurely.
            blown_gap_mult = float(risk_cfg.get("blown_stop_gap_atr_multiplier", 0.0))
            if best_stop > 0 and last < best_stop and best_order is not None:
                order_type_str = str(
                    getattr(best_order, "type", "") or getattr(best_order, "order_type", "")
                ).lower()
                if order_type_str == "stop_limit":
                    gap = best_stop - last
                    atr_threshold = (atr_val or 0.0) * blown_gap_mult
                    if blown_gap_mult > 0 and atr_threshold > 0 and gap < atr_threshold:
                        log_event(
                            f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                            f"old_stop={best_stop:.4f} gap={gap:.4f} atr={float(atr_val or 0):.4f} "
                            f"threshold={atr_threshold:.4f} reason=blown_stop_gap_too_small",
                            event="LIVE",
                        )
                        continue
                    log_event(
                        f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                        f"old_stop={best_stop:.4f} gap={gap:.4f} atr={float(atr_val or 0):.4f} "
                        f"reason=blown_stop_detected",
                        event="LIVE",
                    )
                    # Guard against double-sell after process restart: re-fetch orders
                    # live (not from stale snapshot) so we catch orders placed in this cycle.
                    try:
                        _live_orders_blown = live_api.list_orders(status="open", limit=50)
                    except Exception:
                        _live_orders_blown = open_orders or []
                    _pending_sell = any(
                        getattr(o, "symbol", "") == symbol
                        and str(getattr(o, "side", "")).lower() == "sell"
                        and str(
                            getattr(o, "type", "") or getattr(o, "order_type", "")
                        ).lower() == "market"
                        for o in _live_orders_blown
                    )
                    if _pending_sell:
                        _LIVE_BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _LIVE_BLOWN_STOP_SUPPRESS_SEC
                        log_event(
                            f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                            f"old_stop={best_stop:.4f} reason=blown_stop_already_pending",
                            event="LIVE",
                        )
                        continue
                    if not dry_run:
                        # Cancel ALL open sell orders (stop + any TP limit) and wait
                        # for Alpaca to confirm before submitting market close.
                        # Use the fresh list so snapshot-invisible orders are also cancelled.
                        _cleared = cancel_all_sells_and_wait(live_api, symbol, _live_orders_blown)
                        if not _cleared:
                            log_event(
                                f"LIVE_PROTECT symbol={symbol} reason=blown_stop_cancel_wait_failed",
                                event="LIVE",
                            )
                            send_telegram_alert(
                                f"⚠️ LIVE {symbol}: cancel_wait_timed_out (blown_stop) — sell orders still open after retries. Suppressing 5 min."
                            )
                            # Do NOT attempt market-sell while shares are still locked
                            # in Alpaca sell orders — it will fail with "insufficient qty".
                            _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                                time.monotonic() + _LIVE_NO_STOP_RETRY_SEC
                            )
                            _save_suppress()
                            continue
                        # A TP limit may have partially or fully filled during the
                        # cancel wait. Fetch real position qty before market-selling.
                        _sell_qty = qty
                        try:
                            _sell_qty = float(getattr(live_api.get_position(symbol), "qty", qty))
                        except Exception:
                            pass
                        if _sell_qty <= 0:
                            log_event(
                                f"LIVE_PROTECT symbol={symbol} reason=blown_stop_position_already_closed",
                                event="LIVE",
                            )
                        else:
                            if _sell_qty != qty:
                                log_event(
                                    f"LIVE_PROTECT symbol={symbol} qty_adjusted orig={qty:.0f} real={_sell_qty:.0f} reason=partial_tp_fill",
                                    event="LIVE",
                                )
                                send_telegram_alert(
                                    f"ℹ️ LIVE {symbol}: blown_stop qty adjusted {qty:.0f}→{_sell_qty:.0f} (partial TP fill during cancel window)"
                                )
                            try:
                                client_order_id = f"LIVE.BLOWNSTOP.{symbol}.{int(time.time() * 1000) % 1_000_000}"
                                live_api.submit_order(
                                    symbol=symbol,
                                    side="sell",
                                    qty=_sell_qty,
                                    type="market",
                                    time_in_force="day",
                                    client_order_id=client_order_id,
                                )
                                _LIVE_BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _LIVE_BLOWN_STOP_SUPPRESS_SEC
                                log_event(
                                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                                    f"old_stop={best_stop:.4f} qty={_sell_qty:.0f} reason=blown_stop_market_sell",
                                    event="LIVE",
                                )
                            except Exception as exc:
                                err_str = str(exc)
                                log_event(
                                    f"LIVE_PROTECT symbol={symbol} reason=blown_stop_market_sell_failed err={exc}",
                                    event="LIVE",
                                )
                                # "insufficient qty": force-cancel all live sells and retry once.
                                if "insufficient qty" in err_str.lower():
                                    _retry_cleared = cancel_all_sells_and_wait(live_api, symbol, [])
                                    if _retry_cleared:
                                        try:
                                            _retry_coid = f"LIVE.BLOWNSTOP.RETRY.{symbol}.{int(time.time() * 1000) % 1_000_000}"
                                            live_api.submit_order(
                                                symbol=symbol,
                                                side="sell",
                                                qty=_sell_qty,
                                                type="market",
                                                time_in_force="day",
                                                client_order_id=_retry_coid,
                                            )
                                            _LIVE_BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _LIVE_BLOWN_STOP_SUPPRESS_SEC
                                            log_event(
                                                f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                                                f"old_stop={best_stop:.4f} qty={_sell_qty:.0f} reason=blown_stop_market_sell_retry_ok",
                                                event="LIVE",
                                            )
                                            exc = None  # retry succeeded
                                        except Exception as exc2:
                                            exc = exc2
                                if exc is not None:
                                    send_telegram_alert(
                                        f"🚨 LIVE {symbol}: blown stop — market sell FAILED ({exc}). Position still open below stop at {last:.2f}! Manual close required."
                                    )
                    continue

            tick = tick_ge_1 if last >= 1 else tick_lt_1
            initial_stop_dist = max((atr_val or 0.0) * atr_k, entry * min_stop_pct)
            initial_stop = entry - initial_stop_dist
            denom = max(initial_stop_dist, entry * min_stop_pct)
            r_multiple = (last - entry) / denom if denom > 0 else 0.0

            # Floor at: (1) initial ATR stop, (2) any previously-accepted stop
            # (high-water mark).  The HWM prevents re-placing a stop BELOW a
            # level that was already gained via trailing — stops only ever go up.
            hwm = _LIVE_STOP_HWM.get(symbol, 0.0)
            new_stop = max(best_stop, initial_stop, hwm)
            if best_stop == 0:
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} qty={qty:.0f} "
                    f"initial_stop={initial_stop:.4f} hwm={hwm:.4f} reason=no_stop_found placing_initial_stop",
                    event="LIVE",
                )
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
                in_profit = r_multiple >= trailing_tighten_at_r
                effective_mult = trailing_profit_mult if in_profit else trailing_mult
                trail_stop = last - (atr_val or last * 0.03) * effective_mult
                if trail_stop > new_stop + tick:
                    new_stop = trail_stop
                    reasons.append("trailing_profit" if in_profit else "trailing")

            if new_stop <= best_stop + tick:
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                    f"old_stop={best_stop:.4f} new_stop={new_stop:.4f} reason=skip_no_improve",
                    event="LIVE",
                )
                continue

            if new_stop >= last:
                # If there is no existing stop order and the price has already
                # fallen below the intended stop level, close immediately with
                # a market sell rather than silently skipping the position.
                if best_stop == 0:
                    # Re-fetch orders live — the stale snapshot may miss orders
                    # placed by the TP-renewal pass earlier in this same cycle.
                    try:
                        _live_orders_now = live_api.list_orders(status="open", limit=50)
                    except Exception:
                        _live_orders_now = open_orders or []
                    _pending_sell = any(
                        getattr(o, "symbol", "") == symbol
                        and str(getattr(o, "side", "")).lower() == "sell"
                        for o in _live_orders_now
                    )
                    if _pending_sell:
                        _LIVE_BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _LIVE_BLOWN_STOP_SUPPRESS_SEC
                        log_event(
                            f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                            f"new_stop={new_stop:.4f} reason=no_stop_below_stop_already_pending",
                            event="LIVE",
                        )
                    elif not dry_run:
                        # Cancel ALL open sell orders and wait for Alpaca to confirm
                        # before submitting market close (avoids "insufficient qty").
                        # Use the fresh order list so orders missed by the snapshot are cancelled too.
                        _cleared = cancel_all_sells_and_wait(live_api, symbol, _live_orders_now)
                        if not _cleared:
                            log_event(
                                f"LIVE_PROTECT symbol={symbol} reason=no_stop_cancel_wait_failed",
                                event="LIVE",
                            )
                            send_telegram_alert(
                                f"⚠️ LIVE {symbol}: cancel_wait_timed_out (no_stop) — sell orders still open after retries. Suppressing 5 min."
                            )
                            # Do NOT attempt market-sell while shares are still locked
                            # in Alpaca sell orders — it will fail with "insufficient qty".
                            _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                                time.monotonic() + _LIVE_NO_STOP_RETRY_SEC
                            )
                            _save_suppress()
                            continue
                        # A TP limit may have partially or fully filled during the
                        # cancel wait. Fetch real position qty before market-selling.
                        _sell_qty = qty
                        try:
                            _sell_qty = float(getattr(live_api.get_position(symbol), "qty", qty))
                        except Exception:
                            pass
                        if _sell_qty <= 0:
                            log_event(
                                f"LIVE_PROTECT symbol={symbol} reason=no_stop_position_already_closed",
                                event="LIVE",
                            )
                        else:
                            if _sell_qty != qty:
                                log_event(
                                    f"LIVE_PROTECT symbol={symbol} qty_adjusted orig={qty:.0f} real={_sell_qty:.0f} reason=partial_tp_fill",
                                    event="LIVE",
                                )
                                send_telegram_alert(
                                    f"ℹ️ LIVE {symbol}: no_stop qty adjusted {qty:.0f}→{_sell_qty:.0f} (partial TP fill during cancel window)"
                                )
                            try:
                                client_order_id = f"LIVE.NOSTOP.{symbol}.{int(time.time() * 1000) % 1_000_000}"
                                live_api.submit_order(
                                    symbol=symbol,
                                    side="sell",
                                    qty=_sell_qty,
                                    type="market",
                                    time_in_force="day",
                                    client_order_id=client_order_id,
                                )
                                _LIVE_BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _LIVE_BLOWN_STOP_SUPPRESS_SEC
                                log_event(
                                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                                    f"new_stop={new_stop:.4f} qty={_sell_qty:.0f} reason=no_stop_price_below_stop_market_sell",
                                    event="LIVE",
                                )
                            except Exception as exc:
                                err_str = str(exc)
                                log_event(
                                    f"LIVE_PROTECT symbol={symbol} reason=no_stop_market_sell_failed err={exc}",
                                    event="LIVE",
                                )
                                # "insufficient qty": a sell order appeared between our cancel
                                # and this submission.  Force-cancel all live sells and retry once.
                                if "insufficient qty" in err_str.lower():
                                    _retry_cleared = cancel_all_sells_and_wait(live_api, symbol, [])
                                    if _retry_cleared:
                                        try:
                                            _retry_coid = f"LIVE.NOSTOP.RETRY.{symbol}.{int(time.time() * 1000) % 1_000_000}"
                                            live_api.submit_order(
                                                symbol=symbol,
                                                side="sell",
                                                qty=_sell_qty,
                                                type="market",
                                                time_in_force="day",
                                                client_order_id=_retry_coid,
                                            )
                                            _LIVE_BLOWN_STOP_SUPPRESS[symbol] = time.monotonic() + _LIVE_BLOWN_STOP_SUPPRESS_SEC
                                            log_event(
                                                f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                                                f"new_stop={new_stop:.4f} qty={_sell_qty:.0f} reason=no_stop_market_sell_retry_ok",
                                                event="LIVE",
                                            )
                                            exc = None  # retry succeeded
                                        except Exception as exc2:
                                            exc = exc2
                                if exc is not None:
                                    send_telegram_alert(
                                        f"🚨 LIVE {symbol}: no stop + price at/below stop level — market sell FAILED ({exc}). Position unprotected at {last:.2f}! Manual close required."
                                    )
                    else:
                        log_event(
                            f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                            f"new_stop={new_stop:.4f} reason=no_stop_price_below_stop dry_run=1",
                            event="LIVE",
                        )
                else:
                    log_event(
                        f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                        f"old_stop={best_stop:.4f} new_stop={new_stop:.4f} reason=skip_invalid_stop",
                        event="LIVE",
                    )
                continue

            # Round new_stop to tick size before submitting to Alpaca
            tick_size = get_tick_size(symbol, "us_equity", last)
            new_stop = round_to_tick(float(new_stop), tick_size, mode="down") or new_stop

            reason_txt = "+".join(reasons) if reasons else "update"
            if dry_run:
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                    f"atr={float(atr_val or 0):.4f} old_stop={best_stop:.4f} "
                    f"new_stop={new_stop:.4f} reason={reason_txt} dry_run=1",
                    event="LIVE",
                )
                continue

            # Alpaca rejects stop/stop_limit orders outside the regular session
            # (9:30 AM – 4:00 PM ET).  The existing GTC stop placed during the
            # session stays active and will trigger when the market reopens.
            # Attempting to cancel + replace after hours leaves the position
            # temporarily unprotected if the new placement is rejected.
            if not is_us_equity_market_open():
                log_event(
                    f"LIVE_PROTECT symbol={symbol} old_stop={best_stop:.4f} "
                    f"new_stop={new_stop:.4f} reason=skip_stop_update_after_hours",
                    event="LIVE",
                )
                continue

            stop_payload = {"stop_price": float(new_stop)}
            limit = stop_limit_price(float(new_stop), symbol=symbol)
            order_type = "stop"
            if limit and limit < new_stop:
                stop_payload["limit_price"] = float(limit)
                order_type = "stop_limit"

            if best_order is not None and getattr(best_order, "id", None):
                # Try replace_order first (preferred: keeps bracket chain intact).
                # On failure (e.g. "order chain not fully replaced" from Alpaca),
                # fall back to cancel + new standalone stop.
                # new_stop is already guaranteed > best_stop by the skip logic above,
                # but we take max() as an extra safety to never move the stop down.
                _safe_new_stop = max(float(new_stop), float(best_stop), _LIVE_STOP_HWM.get(symbol, 0.0))
                _safe_payload = {"stop_price": _safe_new_stop}
                _limit = stop_limit_price(_safe_new_stop, symbol=symbol)
                if _limit and _limit < _safe_new_stop:
                    _safe_payload["limit_price"] = float(_limit)
                    order_type = "stop_limit"

                _replaced = False
                try:  # pragma: no cover - network
                    live_api.replace_order(getattr(best_order, "id"), time_in_force=tif, **_safe_payload)
                    _replaced = True
                    # Record this attempt so we can detect async rejections next cycle.
                    _LIVE_LAST_REPLACE[symbol] = (best_stop, _safe_new_stop)
                    _update_stop_hwm(symbol, _safe_new_stop)
                    log_event(
                        f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                        f"atr={float(atr_val or 0):.4f} old_stop={best_stop:.4f} "
                        f"new_stop={_safe_new_stop:.4f} reason={reason_txt}",
                        event="LIVE",
                    )
                except Exception as exc:  # pragma: no cover
                    err_str = str(exc)
                    log_event(
                        f"LIVE_PROTECT symbol={symbol} replace_failed err={err_str} "
                        f"fallback=cancel_resubmit",
                        event="LIVE",
                    )
                    if "insufficient qty" in err_str.lower():
                        _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                            time.monotonic() + _LIVE_INSUF_QTY_SUPPRESS_SEC
                        )
                        _save_suppress()
                    # For any replace failure, fall through to cancel+resubmit.
                    # The cancel+resubmit path sets its own suppress if that also fails.

                if not _replaced:  # pragma: no cover - network
                    # Fallback: cancel old stop, submit new standalone stop.
                    # Only submit if cancel succeeds to avoid duplicate orders.
                    _cancel_ok = False
                    try:
                        live_api.cancel_order(getattr(best_order, "id"))
                        _cancel_ok = True
                    except Exception as exc2:
                        log_event(
                            f"LIVE_PROTECT symbol={symbol} cancel_failed err={exc2} "
                            f"old_stop={best_stop:.4f} kept_as_is=1",
                            event="LIVE",
                        )
                        # Stop can't be cancelled (e.g. bracket leg locked by parent).
                        # Suppress retries to avoid hammering Alpaca every 60 s.
                        _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                            time.monotonic() + _LIVE_NO_STOP_RETRY_SEC
                        )
                        _save_suppress()
                    if _cancel_ok:
                        client_order_id = (
                            f"LIVE.PROTECT.{symbol}"
                            f".{int(_safe_new_stop * 10000)}"
                            f".{int(time.time() * 1000) % 1_000_000}"
                        )
                        try:
                            live_api.submit_order(
                                symbol=symbol,
                                side="sell",
                                qty=qty,
                                type=order_type,
                                time_in_force=tif,
                                client_order_id=client_order_id,
                                **_safe_payload,
                            )
                            _update_stop_hwm(symbol, _safe_new_stop)
                            log_event(
                                f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} "
                                f"last={last:.4f} old_stop={best_stop:.4f} "
                                f"new_stop={_safe_new_stop:.4f} reason={reason_txt}_resubmit",
                                event="LIVE",
                            )
                        except Exception as exc3:
                            log_event(
                                f"LIVE_PROTECT symbol={symbol} resubmit_failed err={exc3}",
                                event="LIVE",
                            )
                            send_telegram_alert(
                                f"⚠️ LIVE {symbol}: stop cancel OK but new stop rejected ({exc3}). Reinstating old stop at {best_stop:.4f}."
                            )
                            # The old stop was cancelled but the new one was rejected.
                            # Immediately reinstate the old stop to avoid leaving the
                            # position unprotected.
                            try:
                                _ri_id = (
                                    f"LIVE.PROTECT.{symbol}"
                                    f".REINSTATE.{int(float(best_stop) * 10000)}"
                                    f".{int(time.time() * 1000) % 1_000_000}"
                                )
                                _ri_payload: dict = {"stop_price": float(best_stop)}
                                _ri_limit = stop_limit_price(float(best_stop), symbol=symbol)
                                _ri_type = "stop"
                                if _ri_limit and _ri_limit < best_stop:
                                    _ri_payload["limit_price"] = float(_ri_limit)
                                    _ri_type = "stop_limit"
                                live_api.submit_order(
                                    symbol=symbol,
                                    side="sell",
                                    qty=qty,
                                    type=_ri_type,
                                    time_in_force=tif,
                                    client_order_id=_ri_id,
                                    **_ri_payload,
                                )
                                log_event(
                                    f"LIVE_PROTECT symbol={symbol} reinstated old_stop={best_stop:.4f}",
                                    event="LIVE",
                                )
                            except Exception as exc4:
                                log_event(
                                    f"LIVE_PROTECT symbol={symbol} reinstate_failed err={exc4}",
                                    event="LIVE",
                                )
                                # Suppress further attempts for 5 min; position may be
                                # unprotected but spamming Alpaca won't help.
                                _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                                    time.monotonic() + _LIVE_NO_STOP_RETRY_SEC
                                )
                                _save_suppress()
                                _now_m = time.monotonic()
                                if _now_m >= _LIVE_STOP_ALERT_COOLDOWN.get(symbol, 0):
                                    send_telegram_alert(
                                        f"🚨 LIVE {symbol}: stop_resubmit AND reinstate BOTH failed — position may be unprotected! old_stop={best_stop:.4f}"
                                    )
                                    _LIVE_STOP_ALERT_COOLDOWN[symbol] = (
                                        _now_m + _LIVE_STOP_ALERT_COOLDOWN_SEC
                                    )
            else:
                # No existing stop order yet — submit a standalone stop.
                # (e.g. position opened outside the bot or bracket already closed)
                #
                # Proactively cancel any open sell orders (e.g. TP limit leg from
                # the original bracket) before submitting so Alpaca does not reject
                # the new stop with "insufficient qty available".
                # Use a fresh order fetch — the stale open_orders snapshot may miss
                # orders placed earlier in this cycle or from a concurrent instance.
                try:
                    _fresh_sells = live_api.list_orders(status="open", limit=50)
                except Exception:
                    _fresh_sells = list(open_orders or [])
                _has_any_sell = any(
                    getattr(o, "symbol", "") == symbol
                    and str(getattr(o, "side", "")).lower() == "sell"
                    for o in _fresh_sells
                )
                if _has_any_sell:
                    _pre_cleared = cancel_all_sells_and_wait(live_api, symbol, _fresh_sells)
                    if not _pre_cleared:
                        log_event(
                            f"LIVE_PROTECT symbol={symbol} reason=no_stop_cancel_pre_failed "
                            f"suppressing={_LIVE_NO_STOP_RETRY_SEC}s",
                            event="LIVE",
                        )
                        _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                            time.monotonic() + _LIVE_NO_STOP_RETRY_SEC
                        )
                        _save_suppress()
                        continue
                client_order_id = f"LIVE.PROTECT.{symbol}.{int(new_stop * 10000)}.{int(time.time() * 1000) % 1_000_000}"
                try:  # pragma: no cover - network
                    live_api.submit_order(
                        symbol=symbol,
                        side="sell",
                        qty=qty,
                        type=order_type,
                        time_in_force=tif,
                        client_order_id=client_order_id,
                        **stop_payload,
                    )
                    _update_stop_hwm(symbol, new_stop)
                    log_event(
                        f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                        f"atr={float(atr_val or 0):.4f} old_stop={best_stop:.4f} "
                        f"new_stop={new_stop:.4f} reason={reason_txt}",
                        event="LIVE",
                    )
                except Exception as exc:  # pragma: no cover
                    err_str = str(exc)
                    if "insufficient qty" in err_str.lower():
                        # One or more sell orders (TP limit, stops) are tying up all
                        # shares. Cancel ALL of them, wait for Alpaca to confirm, then
                        # retry; the TP renewal pass re-places take-profits later.
                        _has_sells = any(
                            getattr(o, "symbol", "") == symbol
                            and str(getattr(o, "side", "")).lower() == "sell"
                            for o in (open_orders or [])
                        )
                        if _has_sells:
                            _cleared = cancel_all_sells_and_wait(live_api, symbol, open_orders)
                            if _cleared:
                                try:
                                    _retry_id = f"LIVE.PROTECT.{symbol}.{int(new_stop * 10000)}.{int(time.time() * 1000) % 1_000_000}"
                                    live_api.submit_order(
                                        symbol=symbol,
                                        side="sell",
                                        qty=qty,
                                        type=order_type,
                                        time_in_force=tif,
                                        client_order_id=_retry_id,
                                        **stop_payload,
                                    )
                                    _update_stop_hwm(symbol, new_stop)
                                    log_event(
                                        f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                                        f"new_stop={new_stop:.4f} reason={reason_txt}_cancel_tp_placed_stop",
                                        event="LIVE",
                                    )
                                except Exception as exc2:
                                    log_event(
                                        f"LIVE_PROTECT symbol={symbol} stop_after_cancel_tp_failed err={exc2}",
                                        event="LIVE",
                                    )
                                    send_telegram_alert(
                                        f"🚨 LIVE {symbol}: cancelled all sells but stop still failed ({exc2}) — position has NO stop and NO TP!"
                                    )
                                    # Position is unprotected: retry in 5 min, not 4 h.
                                    _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                                        time.monotonic() + _LIVE_NO_STOP_RETRY_SEC
                                    )
                                    _save_suppress()
                            else:
                                log_event(
                                    f"LIVE_PROTECT symbol={symbol} cancel_wait_timed_out reason=stop_suppressed",
                                    event="LIVE",
                                )
                                send_telegram_alert(
                                    f"⚠️ LIVE {symbol}: cancel_wait_timed_out (stop placement) — blocking sell orders not cleared. Stop suppressed."
                                )
                                # Blocking sells still open: retry in 5 min.
                                _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                                    time.monotonic() + _LIVE_NO_STOP_RETRY_SEC
                                )
                                _save_suppress()
                        else:
                            _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                                time.monotonic() + _LIVE_INSUF_QTY_SUPPRESS_SEC
                            )
                            _save_suppress()
                    log_event(
                        f"LIVE_PROTECT symbol={symbol} submit_failed err={err_str}",
                        event="LIVE",
                    )
                    # For errors other than "insufficient qty" (which has its own
                    # alert path above), fire a Telegram — no stop protection exists.
                    # Also set a 5-min submission suppress so the bot does NOT
                    # spam Alpaca every 60 s with the same rejected order.
                    if "insufficient qty" not in err_str.lower():
                        # Suppress new stop attempts for 5 min to avoid flooding.
                        _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                            time.monotonic() + _LIVE_NO_STOP_RETRY_SEC
                        )
                        _save_suppress()
                        _now_m = time.monotonic()
                        if _now_m >= _LIVE_STOP_ALERT_COOLDOWN.get(symbol, 0):
                            send_telegram_alert(
                                f"🚨 LIVE {symbol}: stop placement rejected by Alpaca ({err_str}) — position has NO stop! Retrying in 5 min."
                            )
                            _LIVE_STOP_ALERT_COOLDOWN[symbol] = (
                                _now_m + _LIVE_STOP_ALERT_COOLDOWN_SEC
                            )

        # --- Take-profit renewal pass ---
        # Bracket TP legs (limit sells) use day TIF and expire at EOD.
        # Alpaca also silently cancels the TP leg whenever replace_order() is
        # called on the bracket stop leg (even a simple stop-price update).
        # This means after the first trailing-stop tick the position has NO
        # take-profit order and can only exit via the stop.
        # Re-place a standalone GTC limit-sell whenever no open TP exists.
        tp_mult = float(exec_cfg.get("take_profit_atr_mult", 3.0))
        for pos in positions or []:
            try:
                symbol = str(getattr(pos, "symbol", "") or "").upper()
                qty = float(getattr(pos, "qty", 0) or 0)
                side = str(getattr(pos, "side", "") or "").lower()
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            except Exception:
                continue
            if not symbol or qty <= 0 or entry <= 0 or (side and side != "long"):
                continue
            if _is_crypto_symbol(symbol):
                continue
            if time.monotonic() < _LIVE_BLOWN_STOP_SUPPRESS.get(symbol, 0):
                continue
            # Skip if shares are committed to a bracket/stop order.
            if time.monotonic() < _LIVE_INSUF_QTY_SUPPRESS.get(symbol, 0):
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
            last_tp = get_current_price(symbol)
            if not last_tp or last_tp <= 0:
                continue

            computed_tp = round(entry + atr_tp * tp_mult, 2)
            if computed_tp <= last_tp:
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} computed_tp={computed_tp:.2f} "
                    f"last={last_tp:.4f} reason=tp_skip_price_above_target",
                    event="LIVE",
                )
                continue

            if dry_run:
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} computed_tp={computed_tp:.2f} "
                    f"reason=tp_renewal dry_run=1",
                    event="LIVE",
                )
                continue

            try:
                live_api.submit_order(
                    symbol=symbol,
                    side="sell",
                    qty=qty,
                    type="limit",
                    time_in_force="gtc",
                    limit_price=computed_tp,
                    client_order_id=f"LIVE.TP.{symbol}.{int(computed_tp * 100)}.{int(time.time())}",
                )
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last_tp:.4f} "
                    f"atr={atr_tp:.4f} tp={computed_tp:.2f} reason=tp_renewal",
                    event="LIVE",
                )
            except Exception as exc:
                err_str = str(exc)
                if "insufficient qty" in err_str.lower():
                    _LIVE_INSUF_QTY_SUPPRESS[symbol] = (
                        time.monotonic() + _LIVE_INSUF_QTY_SUPPRESS_SEC
                    )
                log_event(
                    f"LIVE_PROTECT symbol={symbol} tp_submit_failed err={exc}",
                    event="LIVE",
                )

    finally:
        if _flock_fd is not None:
            try:
                fcntl.flock(_flock_fd, fcntl.LOCK_UN)
                _flock_fd.close()
            except Exception:
                pass
        _LIVE_PROTECT_LOCK.release()
