import math
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from utils.symbols import detect_asset_class, normalize_for_yahoo


_CACHE_TTL = timedelta(minutes=5)
_stock_cache = {}


class SkipSymbol(Exception):
    """Signal that a symbol should be skipped by upstream callers."""


class YFPricesMissingError(Exception):
    """Raised when Yahoo Finance does not return enough pricing data."""


def fetch_yfinance_stock_data(symbol, verbose: bool = False, return_history: bool = False):
    now = datetime.utcnow()
    cached = _stock_cache.get(symbol)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        if return_history:
            return cached["data"], cached.get("history")
        return cached["data"]
    try:
        asset_class = detect_asset_class(symbol)
        yf_symbol = normalize_for_yahoo(symbol) if asset_class == "preferred" else symbol
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info
        market_cap = info.get("marketCap")
        volume = info.get("volume")
        hist = ticker.history(period="90d", interval="1d")
        if hist.empty or hist["Close"].dropna().empty:
            raise YFPricesMissingError("history_empty")
        weekly_change = None
        if len(hist) >= 2:
            lookback = min(len(hist) - 1, 6)
            base_idx = -lookback - 1
            base_price = hist["Close"].iloc[base_idx]
            if base_price:
                weekly_change = ((hist["Close"].iloc[-1] - base_price) / base_price) * 100
        trend_positive = hist["Close"].iloc[-1] > hist["Close"].iloc[0] if len(hist) >= 2 else None
        price_change_24h = (
            abs((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2]) * 100
            if len(hist) >= 2
            else None
        )
        volume_7d_avg = hist["Volume"].mean() if not hist["Volume"].isna().all() else None

        current_price = hist["Close"].iloc[-1] if not hist.empty else None
        if current_price is None or (isinstance(current_price, float) and math.isnan(current_price)):
            raise YFPricesMissingError("last_close_missing")
        atr = None
        try:
            if len(hist) >= 2 and {"High", "Low", "Close"}.issubset(hist.columns):
                high = hist["High"]
                low = hist["Low"]
                close = hist["Close"]
                prev_close = close.shift(1)
                tr = pd.concat(
                    [
                        high - low,
                        (high - prev_close).abs(),
                        (low - prev_close).abs(),
                    ],
                    axis=1,
                ).max(axis=1)
                atr = tr.rolling(14).mean().iloc[-1]
        except Exception:
            atr = None

        data = (
            market_cap,
            volume,
            weekly_change,
            trend_positive,
            price_change_24h,
            volume_7d_avg,
            current_price,
            atr,
        )
        _stock_cache[symbol] = {"data": data, "history": hist, "ts": now}
        if return_history:
            return data, hist
        return data
    except SkipSymbol:
        raise
    except Exception:
        data = (None, None, None, None, None, None, None, None)
        if return_history:
            return data, None
        return data
