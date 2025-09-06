#alpaca.py

import os
from datetime import datetime, timedelta
import alpaca_trade_api as tradeapi
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
from utils.logger import log_event
from pytz import timezone


load_dotenv()
api = tradeapi.REST(
    os.getenv("APCA_API_KEY_ID"),
    os.getenv("APCA_API_SECRET_KEY"),
    "https://paper-api.alpaca.markets",
    api_version="v2",
)

# Configure basic retry logic on the underlying HTTP session
retry = Retry(total=3, backoff_factor=3)
adapter = HTTPAdapter(max_retries=retry)
api._session.mount("https://", adapter)
api._session.mount("http://", adapter)

# Simple TTL caches to avoid hitting the Alpaca API repeatedly
_market_open_cache = {"ts": None, "value": False}
_price_cache = {}

NY_TZ = timezone("America/New_York")


def supports_bracket_trailing() -> bool:
    """Return whether Alpaca allows trailing stops inside bracket orders."""
    # Alpaca currently does not allow trailing stops within standard brackets.
    return False


def supports_fractional_shares() -> bool:
    """Indicate if Alpaca supports fractional share trading."""
    return True


def _within_regular_hours(now_ny: datetime) -> bool:
    """Return True only during regular trading hours (Mon-Fri, 9:30-16:00 NY)."""
    if now_ny.weekday() >= 5:
        return False
    if now_ny.hour < 9 or (now_ny.hour == 9 and now_ny.minute < 30):
        return False
    if now_ny.hour >= 16:
        return False
    return True


def is_market_open(ttl: int = 60):
    """Check if the market is open, considering regular hours and caching for ``ttl`` seconds."""
    now_utc = datetime.utcnow()
    ts = _market_open_cache["ts"]
    if ts and (now_utc - ts).total_seconds() < ttl:
        return _market_open_cache["value"]
    try:
        clock = api.get_clock()
        now_ny = datetime.now(NY_TZ)
        value = clock.is_open and _within_regular_hours(now_ny)
        _market_open_cache.update({"ts": now_utc, "value": value})
        return value
    except Exception as e:
        log_event(f"❌ Error checking market open: {e}")
        return _market_open_cache["value"]


def get_current_price(symbol, ttl: int = 30):
    """Return the latest minute close for ``symbol`` with a short-lived cache."""
    now = datetime.utcnow()
    cached = _price_cache.get(symbol)
    if cached and (now - cached["ts"]).total_seconds() < ttl:
        return cached["price"]
    try:
        bars = api.get_bars(symbol, tradeapi.TimeFrame.Minute, limit=1)
        if not bars.df.empty:
            price = bars.df['close'].iloc[0]
            _price_cache[symbol] = {"price": price, "ts": now}
            return price
    except Exception as e:
        log_event(f"❌ Error fetching price for {symbol}: {e}")
    return None

