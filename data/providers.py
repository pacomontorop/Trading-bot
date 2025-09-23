"""Live price retrieval with multi-provider fallback and freshness guards."""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Optional, Tuple

import requests
import yfinance as yf
from alpaca_trade_api.rest import TimeFrame

from core.broker import get_tick_size
from libs.broker import ticks as tick_utils
from utils.health import record_price
from utils.logger import log_event
from utils.symbols import detect_asset_class


PriceTuple = Tuple[Optional[Decimal], Optional[datetime], Optional[str], bool, Optional[str]]


PRICE_FRESHNESS_SEC_EQ = int(os.getenv("PRICE_FRESHNESS_SEC_EQ", "300"))
PRICE_FRESHNESS_SEC_CRYPTO = int(os.getenv("PRICE_FRESHNESS_SEC_CRYPTO", "120"))
ALLOW_STALE_EQ_WHEN_OPEN = os.getenv("ALLOW_STALE_EQ_WHEN_OPEN", "false").lower() in {
    "1",
    "true",
    "yes",
}
ALLOW_STALE_EQ_WHEN_CLOSED = os.getenv(
    "ALLOW_STALE_EQ_WHEN_CLOSED", "true"
).lower() in {"1", "true", "yes"}

_CACHE_TTL = 2.0
_cache: Dict[Tuple[str, str], Tuple[float, PriceTuple]] = {}

_EQUITY_TIMEOUT = 2.5
_CRYPTO_TIMEOUT = 1.5
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.2


class PriceProviderError(Exception):
    """Simple error wrapper to store provider-specific failures."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _freshness_limit(kind: str) -> int:
    return PRICE_FRESHNESS_SEC_CRYPTO if kind == "crypto" else PRICE_FRESHNESS_SEC_EQ


def _timeout_for(kind: str) -> float:
    return _CRYPTO_TIMEOUT if kind == "crypto" else _EQUITY_TIMEOUT


def _decimal(value: float | Decimal | str | None) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _round_price(symbol: str, kind: str, price: Decimal | None) -> Optional[Decimal]:
    if price is None:
        return None
    tick_asset = "crypto" if kind == "crypto" else "us_equity"
    tick_size = get_tick_size(symbol, tick_asset, float(price))
    if not tick_size or tick_size <= 0:
        return price
    tick_dec = _decimal(tick_size)
    if tick_dec is None or tick_dec <= 0:
        return price
    return tick_utils.round_to_tick(price, tick_dec, mode="NEAREST")


def _retry_call(
    func: Callable[[], Tuple[Optional[Decimal], Optional[datetime]]],
    timeout: float,
) -> Tuple[Optional[Decimal], Optional[datetime]]:
    delay = _RETRY_BASE_DELAY
    last_exc: Optional[Exception] = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt == _RETRY_ATTEMPTS - 1:
                raise PriceProviderError(str(exc))
            sleep_for = delay + random.uniform(0, delay)
            time.sleep(min(sleep_for, timeout))
            delay *= 2
    raise PriceProviderError(str(last_exc) if last_exc else "unknown_error")


def _alpaca_price(symbol: str, kind: str) -> Tuple[Optional[Decimal], Optional[datetime]]:
    from broker import alpaca as alpaca_mod

    alpaca_api = alpaca_mod.api
    if kind == "crypto":
        bars = alpaca_api.get_crypto_bars(symbol, TimeFrame.Minute, limit=1)
    else:
        bars = alpaca_api.get_bars(symbol, TimeFrame.Minute, limit=1)
    df = getattr(bars, "df", None)
    if df is None or df.empty:
        return None, None
    row = df.iloc[-1]
    ts_index = df.index[-1]
    if isinstance(ts_index, tuple):
        ts_index = ts_index[1]
    if hasattr(ts_index, "to_pydatetime"):
        ts = ts_index.to_pydatetime()
    else:
        ts = datetime.fromisoformat(str(ts_index))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    price = _decimal(row.get("close") or row.get("c"))
    return price, ts


def _polygon_price(symbol: str, kind: str) -> Tuple[Optional[Decimal], Optional[datetime]]:
    key = os.getenv("POLYGON_API_KEY")
    if not key:
        raise PriceProviderError("missing_key")
    if kind == "crypto":
        endpoint = f"https://api.polygon.io/v1/last/crypto/{symbol.upper()}"
    else:
        endpoint = f"https://api.polygon.io/v2/last/trade/{symbol.upper()}"
    timeout = _timeout_for(kind)

    def _call() -> Tuple[Optional[Decimal], Optional[datetime]]:
        resp = requests.get(endpoint, params={"apiKey": key}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("results") or data.get("last")
        if not result:
            return None, None
        price = result.get("p") or result.get("price") or result.get("close")
        ts_ns = result.get("t") or result.get("timestamp")
        if ts_ns is None:
            ts = None
        else:
            ts = datetime.fromtimestamp(int(ts_ns) / 1_000_000_000, tz=timezone.utc)
        return _decimal(price), ts

    return _retry_call(_call, timeout)


def _finnhub_price(symbol: str, kind: str) -> Tuple[Optional[Decimal], Optional[datetime]]:
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        raise PriceProviderError("missing_key")
    timeout = _timeout_for(kind)

    def _call() -> Tuple[Optional[Decimal], Optional[datetime]]:
        resp = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": symbol, "token": key},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        price = data.get("c")
        ts = data.get("t")
        if ts:
            ts_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        else:
            ts_dt = None
        return _decimal(price), ts_dt

    return _retry_call(_call, timeout)


def _alphavantage_price(symbol: str, kind: str) -> Tuple[Optional[Decimal], Optional[datetime]]:
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    if not key:
        raise PriceProviderError("missing_key")
    timeout = _timeout_for(kind)

    def _call() -> Tuple[Optional[Decimal], Optional[datetime]]:
        resp = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "TIME_SERIES_INTRADAY",
                "symbol": symbol,
                "interval": "1min",
                "apikey": key,
                "datatype": "json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        series = data.get("Time Series (1min)")
        if not series:
            raise PriceProviderError(data.get("Note") or "no_series")
        latest_ts = sorted(series.keys())[-1]
        bar = series[latest_ts]
        price = _decimal(bar.get("4. close"))
        ts_dt = datetime.fromisoformat(latest_ts).replace(tzinfo=timezone.utc)
        return price, ts_dt

    return _retry_call(_call, timeout)


def _yahoo_price(symbol: str, kind: str) -> Tuple[Optional[Decimal], Optional[datetime]]:
    timeout = _timeout_for(kind)

    def _call() -> Tuple[Optional[Decimal], Optional[datetime]]:
        ticker = yf.Ticker(symbol)
        info = getattr(ticker, "fast_info", None)
        price = None
        if info and hasattr(info, "lastPrice"):
            price = info.lastPrice
        if price is None:
            hist = ticker.history(period="1d", interval="1m")
            if not hist.empty:
                price = hist["Close"].iloc[-1]
                ts = hist.index[-1].to_pydatetime()
            else:
                raise PriceProviderError("yahoo_empty")
        else:
            ts = _now_utc()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return _decimal(price), ts

    return _retry_call(_call, timeout)


_PROVIDERS: Tuple[Tuple[str, Callable[[str, str], Tuple[Optional[Decimal], Optional[datetime]]]], ...] = (
    ("alpaca", _alpaca_price),
    ("polygon", _polygon_price),
    ("finnhub", _finnhub_price),
    ("alphavantage", _alphavantage_price),
    ("yahoo", _yahoo_price),
)


def _record_stat(price: Optional[Decimal], stale: bool) -> None:
    if price is None:
        record_price("failed")
    elif stale:
        record_price("stale")
    else:
        record_price("ok")


def get_price(
    symbol: str,
    kind: str | None = None,
    *,
    market_open: Optional[bool] = None,
    allow_stale_open: Optional[bool] = None,
    allow_stale_closed: Optional[bool] = None,
) -> PriceTuple:
    """Return ``(price, ts, provider, stale, stale_reason)`` for ``symbol``."""

    if not symbol:
        return None, None, None, False, "symbol_empty"

    upper = symbol.upper()
    inferred_kind = kind or detect_asset_class(upper)
    inferred_kind = "crypto" if inferred_kind == "crypto" else "equity"

    cache_key = (upper, inferred_kind)
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        price, ts, provider, stale, reason = cached[1]
        _record_stat(price, stale)
        return price, ts, provider, stale, reason

    if market_open is None and inferred_kind == "equity":
        try:
            from core.market_gate import is_us_equity_market_open

            market_open = is_us_equity_market_open()
        except Exception:
            market_open = None

    if allow_stale_open is None:
        allow_stale_open = ALLOW_STALE_EQ_WHEN_OPEN
    if allow_stale_closed is None:
        allow_stale_closed = ALLOW_STALE_EQ_WHEN_CLOSED

    max_age = _freshness_limit(inferred_kind)
    reasons: Dict[str, str] = {}

    for name, provider in _PROVIDERS:
        try:
            timeout = _timeout_for(inferred_kind)

            def _wrapped() -> Tuple[Optional[Decimal], Optional[datetime]]:
                return provider(upper, inferred_kind)

            price, ts = _retry_call(_wrapped, timeout)
        except PriceProviderError as exc:
            reasons[name] = str(exc)
            continue
        except Exception as exc:  # pragma: no cover - defensive catch
            reasons[name] = str(exc)
            continue

        if price is None or ts is None:
            reasons[name] = "no_data"
            continue

        price = _round_price(upper, inferred_kind, price)
        if price is None:
            reasons[name] = "rounding_failed"
            continue

        if price.is_nan():
            reasons[name] = "price_nan"
            continue

        if price <= 0:
            reasons[name] = "price<=0"
            continue

        age = (_now_utc() - ts).total_seconds()
        stale = bool(age > max_age)
        stale_reason = f"stale>{max_age}" if stale else None

        if stale and inferred_kind == "equity":
            allowed = (market_open is True and allow_stale_open) or (
                market_open in (False, None) and allow_stale_closed
            )
            if not allowed:
                reasons[name] = stale_reason or "stale"
                continue

        result: PriceTuple = (price, ts, name, stale, stale_reason)
        _cache[cache_key] = (now, result)
        _record_stat(price, stale)
        return result

    failure_text = "no_price"
    if reasons:
        failure_text = ",".join(f"{k}:{v}" for k, v in reasons.items())
        log_event(
            f"PRICE_FAIL symbol={upper} reasons={reasons}",
            event="ERROR",
        )

    result = (None, None, None, False, failure_text)
    _cache[cache_key] = (now, result)
    _record_stat(None, False)
    return result
