from __future__ import annotations
"""Helper utilities to enforce daily notional limits for crypto trading.

The limit is defined as 10% of the available buying power and resets when the
U.S. equities market opens on the next trading day.  It uses Alpaca's clock API
so weekends and market holidays are respected automatically.
"""

from datetime import datetime
from threading import Lock

from pytz import timezone

def get_api():
    from broker.alpaca import api as real_api
    return real_api


NY = timezone("America/New_York")


class CryptoLimit:
    """Track how much capital has been used for crypto in the current session."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.max_notional = 0.0
        self._spent = 0.0
        self._reset_time = datetime.now(NY)
        self.reset()

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Recalculate limit and reset the spent tracker."""
        api = get_api()
        acct = api.get_account()
        self.max_notional = float(acct.buying_power) * 0.10
        self._spent = 0.0
        clock = api.get_clock()
        # ``next_open`` already accounts for weekends/holidays
        self._reset_time = clock.next_open.astimezone(NY)

    # ------------------------------------------------------------------
    def check_reset(self) -> None:
        """Reset the counter if we've crossed into a new trading day."""
        if datetime.now(NY) >= self._reset_time:
            self.reset()

    # ------------------------------------------------------------------
    def can_spend(self, amount: float) -> bool:
        """Return ``True`` and deduct ``amount`` if within today's limit."""
        with self._lock:
            self.check_reset()
            if self._spent + amount > self.max_notional:
                return False
            self._spent += amount
            return True

    # ------------------------------------------------------------------
    def remaining(self) -> float:
        """Return remaining notional before hitting the daily cap."""
        with self._lock:
            self.check_reset()
            return self.max_notional - self._spent

    # ------------------------------------------------------------------
    @property
    def spent(self) -> float:
        with self._lock:
            self.check_reset()
            return self._spent


_singleton: CryptoLimit | None = None


def get_crypto_limit() -> CryptoLimit:
    """Return a lazily-initialized :class:`CryptoLimit` instance."""
    global _singleton
    if _singleton is None:
        _singleton = CryptoLimit()
    return _singleton
