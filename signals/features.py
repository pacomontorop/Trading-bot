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


def get_symbol_features(
    symbol: str,
    *,
    yahoo_snapshot=None,
    yahoo_hist=None,
    yahoo_symbol: str | None = None,
    quiver_symbol: str | None = None,
    quiver_fallback_symbol: str | None = None,
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

    # Technical indicators: RSI, SMA crossovers, momentum, volume spike.
    # These provide buy signals when Quiver data is absent or weak.
    if config.ENABLE_YAHOO and yahoo_hist is not None and current_price:
        from signals.scoring import compute_technical_features
        tech = compute_technical_features(yahoo_hist, float(current_price))
        features.update(tech)

    return {key: _to_numeric(value) for key, value in features.items()}
