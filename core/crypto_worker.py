"""Background worker for trading cryptocurrencies outside equity market hours."""

import threading
import time

from broker.alpaca import api, is_market_open
from signals.crypto_signals import get_crypto_signals
from utils.crypto_limit import get_crypto_limit
from utils.logger import log_event


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


def crypto_worker() -> None:
    """Continuously scan for crypto signals when equities market is closed."""
    log_event("🪙 Crypto worker started")
    while True:
        if is_market_open():
            # Sleep while equities market is open
            crypto_limit.check_reset()
            time.sleep(60)
            continue

        crypto_limit.check_reset()
        remaining = crypto_limit.remaining()
        if remaining <= 0:
            time.sleep(60)
            continue

        account = api.get_account()
        # Consider both cash and margin buying power
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
            log_event("⚠️ No USD available for crypto trades")
            time.sleep(60)
            continue

        signals = get_crypto_signals()
        for symbol, score in signals:
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
                available_funds -= alloc
                with crypto_trades_lock:
                    crypto_trades.append(f"{symbol} ${alloc:.2f}")
                log_event(f"🪙 Executed {symbol} for {alloc:.2f} USD")
            except Exception as e:
                log_event(f"❌ Crypto order failed for {symbol}: {e}")
            time.sleep(1)
        time.sleep(60)
