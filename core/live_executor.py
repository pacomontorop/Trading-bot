"""Order placement and position protection for the live (real-money) Alpaca account.

``place_live_order`` submits a bracket buy via the live API.
``tick_protect_live_positions`` runs break-even and trailing-stop upgrades
for all open live long positions (same logic as the paper protector).
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import config
from broker.alpaca import get_current_price
from broker.alpaca_live import live_api, list_live_positions, list_live_open_orders
from core.broker import get_tick_size, round_to_tick
from core.order_protection import compute_bracket_prices, stop_limit_price, validate_bracket_prices
from core.safeguards import is_safeguards_active
from utils.logger import log_event

_LIVE_PROTECT_LOCK = threading.Lock()
_ATR_CACHE: dict[str, tuple[float, float]] = {}
_ATR_TTL_SEC = 300.0

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
    qty = float(plan.get("qty") or 0)
    price = float(plan.get("price") or 0)
    atr = plan.get("atr")
    time_in_force = plan.get("time_in_force", "day")

    if qty <= 0 or not symbol:
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

    try:
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
        atr_k = float(risk_cfg.get("atr_k", 2.0))
        min_stop_pct = float(risk_cfg.get("min_stop_pct", 0.05))
        tick_ge_1 = float(risk_cfg.get("min_tick_equity_ge_1", 0.01))
        tick_lt_1 = float(risk_cfg.get("min_tick_equity_lt_1", 0.0001))
        tif = exec_cfg.get("time_in_force", "day")

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
            if _is_crypto_symbol(symbol):
                log_event(
                    f"LIVE_PROTECT symbol={symbol} reason=skip_crypto",
                    event="LIVE",
                )
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

            if not last or last <= 0:
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last=0 reason=skip_no_price",
                    event="LIVE",
                )
                continue

            # --- Blown stop detection ---
            # If last price is already below the existing stop-limit order and
            # the order is still open, the stop was bypassed (price gapped).
            # Cancel the stuck stop-limit and place an immediate market sell.
            if best_stop > 0 and last < best_stop and best_order is not None:
                order_type_str = str(
                    getattr(best_order, "type", "") or getattr(best_order, "order_type", "")
                ).lower()
                if order_type_str == "stop_limit":
                    log_event(
                        f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                        f"old_stop={best_stop:.4f} reason=blown_stop_detected",
                        event="LIVE",
                    )
                    if not dry_run:
                        try:
                            live_api.cancel_order(getattr(best_order, "id"))
                        except Exception:
                            pass
                        try:
                            client_order_id = f"LIVE.BLOWNSTOP.{symbol}.{int(time.time() * 1000) % 1_000_000}"
                            live_api.submit_order(
                                symbol=symbol,
                                side="sell",
                                qty=qty,
                                type="market",
                                time_in_force="day",
                                client_order_id=client_order_id,
                            )
                            log_event(
                                f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                                f"old_stop={best_stop:.4f} reason=blown_stop_market_sell",
                                event="LIVE",
                            )
                        except Exception as exc:
                            log_event(
                                f"LIVE_PROTECT symbol={symbol} reason=blown_stop_market_sell_failed err={exc}",
                                event="LIVE",
                            )
                    continue

            tick = tick_ge_1 if last >= 1 else tick_lt_1
            initial_stop_dist = max((atr_val or 0.0) * atr_k, entry * min_stop_pct)
            denom = max(initial_stop_dist, entry * min_stop_pct)
            r_multiple = (last - entry) / denom if denom > 0 else 0.0

            new_stop = best_stop
            reasons: list[str] = []

            if r_multiple >= break_even_r:
                be_stop = entry * (1 + break_even_buffer)
                if be_stop > new_stop + tick:
                    new_stop = be_stop
                    reasons.append("break_even")

            if trailing_enable:
                trail_stop = last - (atr_val or last * 0.03) * trailing_mult
                if trail_stop > new_stop + tick:
                    new_stop = trail_stop
                    reasons.append("trailing")

            if new_stop <= best_stop + tick:
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                    f"old_stop={best_stop:.4f} new_stop={new_stop:.4f} reason=skip_no_improve",
                    event="LIVE",
                )
                continue

            if new_stop >= last:
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

            try:
                if best_order is not None and getattr(best_order, "id", None):
                    live_api.cancel_order(getattr(best_order, "id"))
            except Exception as exc:
                log_event(
                    f"LIVE_PROTECT symbol={symbol} cancel_failed err={exc}",
                    event="LIVE",
                )
                continue

            stop_payload = {"stop_price": float(new_stop)}
            limit = stop_limit_price(float(new_stop), symbol=symbol)
            order_type = "stop"
            if limit and limit < new_stop:
                stop_payload["limit_price"] = float(limit)
                order_type = "stop_limit"

            client_order_id = f"LIVE.PROTECT.{symbol}.{int(new_stop * 10000)}.{int(time.time())}"
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
                log_event(
                    f"LIVE_PROTECT symbol={symbol} entry={entry:.4f} last={last:.4f} "
                    f"atr={float(atr_val or 0):.4f} old_stop={best_stop:.4f} "
                    f"new_stop={new_stop:.4f} reason={reason_txt}",
                    event="LIVE",
                )
            except Exception as exc:  # pragma: no cover
                log_event(
                    f"LIVE_PROTECT symbol={symbol} submit_failed err={exc}",
                    event="LIVE",
                )
    finally:
        _LIVE_PROTECT_LOCK.release()
