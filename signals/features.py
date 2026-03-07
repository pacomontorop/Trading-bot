"""Aggregate numeric features from all providers for scoring.

This layer only converts raw data into numbers. It intentionally avoids
thresholds or decisions so scoring stays centralized.
"""

from __future__ import annotations

from typing import Any

import config


def _to_numeric(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def compute_rsi_from_hist(hist, period: int = 14) -> float | None:
    """Compute RSI from an already-fetched yfinance history DataFrame.

    Reuses data already downloaded by scoring.py — no extra API call.
    Returns None if there is insufficient data.
    """
    if hist is None or hist.empty or len(hist) < period + 1:
        return None
    try:
        import pandas as pd  # noqa: PLC0415

        close = hist["Close"].astype(float)
        delta = close.diff().dropna()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        latest = rsi.iloc[-1]
        if isinstance(latest, pd.Series):
            latest = latest.iloc[0]
        return float(latest) if not __import__("math").isnan(float(latest)) else None
    except Exception:
        return None


def get_symbol_features(
    symbol: str,
    *,
    yahoo_snapshot=None,
    yahoo_symbol: str | None = None,
    quiver_symbol: str | None = None,
    quiver_fallback_symbol: str | None = None,
    yahoo_hist=None,
) -> dict[str, float]:
    """Return a flat numeric feature dict for ``symbol`` from all providers."""
    features: dict[str, float | int | None] = {}

    if config.ENABLE_QUIVER:
        from signals import quiver_utils

        quiver_features = quiver_utils.fetch_quiver_signals(
            quiver_symbol or symbol,
            fallback_symbol=quiver_fallback_symbol,
        )
        features.update(quiver_features or {})

    if config.ENABLE_YAHOO:
        from signals.scoring import fetch_yahoo_snapshot, SkipSymbol, YFPricesMissingError

        try:
            if yahoo_snapshot is None:
                snapshot = fetch_yahoo_snapshot(
                    symbol,
                    yahoo_symbol=yahoo_symbol,
                    fallback_symbol=symbol if yahoo_symbol else None,
                )
                yahoo_snapshot = snapshot.data
            (
                market_cap,
                volume,
                weekly_change,
                trend_positive,
                price_change_24h,
                volume_7d,
                current_price,
                atr,
            ) = yahoo_snapshot
        except (SkipSymbol, YFPricesMissingError):
            market_cap = (
                volume
            ) = (
                weekly_change
            ) = (
                trend_positive
            ) = price_change_24h = volume_7d = current_price = atr = 0.0
    else:
        market_cap = volume = weekly_change = trend_positive = price_change_24h = volume_7d = current_price = atr = 0.0
    features["yahoo_market_cap"] = market_cap
    features["yahoo_volume"] = volume
    features["yahoo_weekly_change_pct"] = weekly_change
    features["yahoo_trend_positive"] = trend_positive
    features["yahoo_price_change_24h_pct"] = price_change_24h
    features["yahoo_volume_7d_avg"] = volume_7d
    features["yahoo_current_price"] = current_price
    features["yahoo_atr"] = atr
    if current_price and atr:
        features["yahoo_atr_pct"] = (float(atr) / float(current_price)) * 100.0
    else:
        features["yahoo_atr_pct"] = 0.0

    # Technical features — computed from already-fetched history (no extra API call)
    rsi = compute_rsi_from_hist(yahoo_hist)
    features["yahoo_rsi_14"] = rsi if rsi is not None else 0.0

    if config.ENABLE_YAHOO and yahoo_hist is not None:
        from signals.scoring import compute_technical_features
        cp = _to_numeric(current_price)
        if cp > 0:
            tech = compute_technical_features(yahoo_hist, cp)
            features.update(tech)

    # Insider net: positive = more buys than sells (cleaner long signal)
    buy_count = _to_numeric(features.get("quiver_insider_buy_count"))
    sell_count = _to_numeric(features.get("quiver_insider_sell_count"))
    features["quiver_insider_net_count"] = buy_count - sell_count

    return {key: _to_numeric(value) for key, value in features.items()}
