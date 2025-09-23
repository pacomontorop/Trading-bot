"""Background worker for trading cryptocurrencies outside equity market hours."""

import threading
import time

import threading
import time

from alpaca_trade_api.rest import APIError

from broker.alpaca import api, is_market_open
from signals.crypto_signals import get_crypto_signals
from utils.crypto_limit import get_crypto_limit
from utils.logger import log_event
from utils.health import record_scan


# Thread-safe list of executed crypto trades for daily summaries
crypto_trades = []
crypto_trades_lock = threading.Lock()
crypto_limit = get_crypto_limit()


def _calculate_allocation(score: int) -> float:
    """Map a signal score to a notional allocation."""
    bp = crypto_limit.max_notional
    if score >= 90:
        return bp * 0.03
    if score >= 80:
        return bp * 0.02
    return bp * 0.01


def crypto_worker(stop_event: threading.Event) -> None:
    """Continuously scan for crypto signals only when the equities market is closed."""
    log_event("🪙 Crypto worker started")
    while not stop_event.is_set():
        if is_market_open():
            break

        crypto_limit.check_reset()
        remaining = crypto_limit.remaining()
        if remaining <= 0:
            time.sleep(60)
            continue

        account = api.get_account()
        cash_available = float(
            getattr(
                account,
                "non_marginable_buying_power",
                getattr(account, "cash", 0),
            )
        )
        margin_available = float(getattr(account, "buying_power", 0))
        available_funds = max(cash_available, margin_available)
        if available_funds <= 0:
            log_event("⚠️ No USD buying power for crypto trades")
            time.sleep(60)
            continue

        signals = get_crypto_signals()
        record_scan("crypto", len(signals))
        for symbol, score in signals:
            # Skip if a position already exists for this symbol
            try:
                api.get_position(symbol)
                log_event(
                    f"Position already open for {symbol}, skipping",
                    event="DEBUG",
                    symbol=symbol,
                    dedupe_key=("open_position", symbol),
                    dedupe_ttl=45,
                )
                continue
            except APIError:
                pass

            raw_alloc = min(
                _calculate_allocation(score),
                crypto_limit.remaining(),
                available_funds,
            )
            alloc = round(raw_alloc, 2)
            if alloc <= 0 or not crypto_limit.can_spend(alloc):
                continue
            try:
                api.submit_order(
                    symbol=symbol,
                    notional=alloc,
                    side="buy",
                    type="market",
                    time_in_force="ioc",
                )
                # Allow fill before placing protective stop
                time.sleep(1)
                try:
                    position = api.get_position(symbol)
                    qty = abs(float(position.qty))
                    stop_price = round(float(position.avg_entry_price) * 0.95, 2)
                    api.submit_order(
                        symbol=symbol,
                        qty=qty,
                        side="sell",
                        type="stop_limit",
                        time_in_force="gtc",
                        stop_price=stop_price,
                        limit_price=stop_price,
                    )
                    log_event(f"🔒 Stop loss for {symbol} at {stop_price}")
                except Exception as e:
                    log_event(f"⚠️ Failed to set stop loss for {symbol}: {e}")
                available_funds -= alloc
                with crypto_trades_lock:
                    crypto_trades.append(f"{symbol} ${alloc:.2f}")
                log_event(f"🪙 Executed {symbol} for {alloc:.2f} USD")
            except Exception as e:
                log_event(f"❌ Crypto order failed for {symbol}: {e}")
            time.sleep(1)
        time.sleep(60)
    log_event("🛑 Crypto worker stopped")
