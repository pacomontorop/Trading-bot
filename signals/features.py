"""Aggregate numeric features from all providers for scoring.

This layer only converts raw data into numbers. It intentionally avoids
thresholds or decisions so scoring stays centralized.
"""

from __future__ import annotations

from typing import Any

import config


_GRADE_MAPPING = {
    "strong buy": 2.0,
    "buy": 1.0,
    "outperform": 1.0,
    "overweight": 1.0,
    "hold": 0.0,
    "neutral": 0.0,
    "sell": -1.0,
    "underperform": -1.0,
    "underweight": -1.0,
    "strong sell": -2.0,
}


def _to_numeric(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _grade_value(symbol: str) -> float:
    from signals import fmp_utils

    data = fmp_utils.grades_news(symbol, limit=1)
    if not data:
        return 0.0
    grade = (data[0].get("newGrade") or "").lower()
    return float(_GRADE_MAPPING.get(grade, 0.0))


def get_symbol_features(symbol: str) -> dict[str, float]:
    """Return a flat numeric feature dict for ``symbol`` from all providers."""
    features: dict[str, float | int | None] = {}

    if config.ENABLE_QUIVER:
        from signals import quiver_utils

        quiver_features = quiver_utils.fetch_quiver_signals(symbol)
        features.update(quiver_features or {})

    if config.ENABLE_FMP:
        # FMP booster only — intentionally minimal
        features["fmp_grade_score"] = _grade_value(symbol)

        from signals import fmp_utils

        rating = fmp_utils.ratings_snapshot(symbol)
        if rating:
            features["fmp_rating_overall_score"] = rating[0].get("overallScore")

    if config.ENABLE_YAHOO:
        from signals.scoring import fetch_yfinance_stock_data, SkipSymbol, YFPricesMissingError

        try:
            (
                market_cap,
                volume,
                weekly_change,
                trend_positive,
                price_change_24h,
                volume_7d,
                current_price,
                atr,
            ) = fetch_yfinance_stock_data(symbol)
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

    return {key: _to_numeric(value) for key, value in features.items()}
