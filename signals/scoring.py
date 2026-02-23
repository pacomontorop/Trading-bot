import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict

import pandas as pd
import yfinance as yf

import config
from utils.symbols import detect_asset_class, normalize_for_yahoo


_CACHE_TTL = timedelta(minutes=5)
_stock_cache = {}


class SkipSymbol(Exception):
    """Signal that a symbol should be skipped by upstream callers."""


class YFPricesMissingError(Exception):
    """Raised when Yahoo Finance does not return enough pricing data."""


@dataclass
class YahooSnapshot:
    data: tuple
    used_symbol: str
    fallback_used: bool
    status: str


def _fetch_yahoo_data(symbol: str, return_history: bool = False):
    now = datetime.utcnow()
    cached = _stock_cache.get(symbol)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        if return_history:
            return cached["data"], cached.get("history")
        return cached["data"]
    ticker = yf.Ticker(symbol)
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
    volume_7d_avg = hist["Volume"].tail(7).mean() if not hist["Volume"].isna().all() else None

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


def fetch_yahoo_snapshot(
    symbol: str,
    *,
    yahoo_symbol: str | None = None,
    fallback_symbol: str | None = None,
    return_history: bool = False,
) -> YahooSnapshot | tuple[YahooSnapshot, pd.DataFrame | None]:
    if not config.ENABLE_YAHOO:
        snapshot = YahooSnapshot((None, None, None, None, None, None, None, None), symbol, False, "disabled")
        return (snapshot, None) if return_history else snapshot
    primary = yahoo_symbol or symbol
    try:
        data = _fetch_yahoo_data(primary, return_history=return_history)
        if return_history:
            data_tuple, hist = data
        else:
            data_tuple, hist = data, None
        snapshot = YahooSnapshot(data_tuple, primary, False, "ok")
        return (snapshot, hist) if return_history else snapshot
    except Exception:
        if fallback_symbol and fallback_symbol != primary:
            try:
                data = _fetch_yahoo_data(fallback_symbol, return_history=return_history)
                if return_history:
                    data_tuple, hist = data
                else:
                    data_tuple, hist = data, None
                snapshot = YahooSnapshot(data_tuple, fallback_symbol, True, "ok")
                return (snapshot, hist) if return_history else snapshot
            except Exception:
                pass
        snapshot = YahooSnapshot((None, None, None, None, None, None, None, None), primary, False, "missing")
        return (snapshot, None) if return_history else snapshot


def _rsi_signal_score(rsi: float) -> float:
    """Convert RSI value to a 0–2 buy-signal score.

    RSI 30–50  → recovering from oversold / healthy accumulation → 1.0–1.5
    RSI 50–65  → trending up with momentum → 1.0
    RSI 65–75  → extended but still OK → 0.5
    RSI > 75   → overbought, avoid → 0.0
    RSI < 30   → falling knife, weak bounce signal → 0.5
    """
    if rsi <= 0 or rsi >= 100 or math.isnan(rsi):
        return 0.0
    if rsi < 30:
        return 0.5
    if rsi <= 50:
        # Linear scale 1.0 (at RSI=50) → 1.5 (at RSI=30)
        return 1.0 + (50.0 - rsi) / 40.0
    if rsi <= 65:
        return 1.0
    if rsi <= 75:
        return 0.5
    return 0.0


def compute_technical_features(hist: pd.DataFrame, current_price: float) -> Dict[str, float]:
    """Compute RSI, SMA, momentum and volume-spike features from 90-day OHLCV history.

    Returns a flat dict of ``yahoo_*`` feature keys with float values.
    All features default to 0.0 when there is insufficient data.
    """
    features: Dict[str, float] = {
        "yahoo_rsi_14": 50.0,
        "yahoo_rsi_signal": 0.0,
        "yahoo_above_sma20": 0.0,
        "yahoo_above_sma50": 0.0,
        "yahoo_momentum_20d_pct": 0.0,
        "yahoo_volume_spike_ratio": 1.0,
    }

    if hist is None or hist.empty or current_price <= 0:
        return features

    close = hist["Close"].dropna()
    if len(close) < 2:
        return features

    # RSI-14
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    with pd.option_context("mode.use_inf_as_na", True):
        rs = gain / loss
    rsi_series = 100.0 - (100.0 / (1.0 + rs))
    rsi_val = float(rsi_series.iloc[-1]) if not rsi_series.empty else 50.0
    if math.isnan(rsi_val):
        rsi_val = 50.0
    features["yahoo_rsi_14"] = rsi_val
    features["yahoo_rsi_signal"] = _rsi_signal_score(rsi_val)

    # SMA-20 and SMA-50
    if len(close) >= 20:
        sma20 = float(close.rolling(20).mean().iloc[-1])
        if not math.isnan(sma20) and sma20 > 0:
            features["yahoo_above_sma20"] = 1.0 if current_price > sma20 else 0.0

    if len(close) >= 50:
        sma50 = float(close.rolling(50).mean().iloc[-1])
        if not math.isnan(sma50) and sma50 > 0:
            features["yahoo_above_sma50"] = 1.0 if current_price > sma50 else 0.0

    # 20-day price momentum (%)
    if len(close) >= 20:
        price_20d_ago = float(close.iloc[-20])
        if price_20d_ago > 0:
            features["yahoo_momentum_20d_pct"] = (current_price - price_20d_ago) / price_20d_ago * 100.0

    # Volume spike: recent 5-day average vs 90-day average
    if "Volume" in hist.columns:
        vol = hist["Volume"].dropna()
        if len(vol) >= 5:
            avg_vol = float(vol.mean())
            recent_vol = float(vol.tail(5).mean())
            if avg_vol > 0:
                features["yahoo_volume_spike_ratio"] = recent_vol / avg_vol

    return features


def fetch_yfinance_stock_data(
    symbol,
    verbose: bool = False,
    return_history: bool = False,
    yahoo_symbol: str | None = None,
    fallback_symbol: str | None = None,
):
    if not config.ENABLE_YAHOO:
        data = (None, None, None, None, None, None, None, None)
        if return_history:
            return data, None
        return data
    try:
        asset_class = detect_asset_class(symbol)
        yf_symbol = yahoo_symbol or (normalize_for_yahoo(symbol) if asset_class == "preferred" else symbol)
        snapshot = fetch_yahoo_snapshot(
            symbol,
            yahoo_symbol=yf_symbol,
            fallback_symbol=fallback_symbol,
            return_history=return_history,
        )
        if return_history:
            snapshot, hist = snapshot
            return snapshot.data, hist
        return snapshot.data
    except SkipSymbol:
        raise
    except Exception:
        data = (None, None, None, None, None, None, None, None)
        if return_history:
            return data, None
        return data
