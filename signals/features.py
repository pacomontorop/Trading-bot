"""Aggregate numeric features from all providers for scoring.

This layer only converts raw data into numbers. It intentionally avoids
thresholds or decisions so scoring stays centralized.
"""

from __future__ import annotations

from typing import Any

from signals import quiver_utils
from signals.fmp_signals import BULLISH_KEYWORDS, BEARISH_KEYWORDS
from signals.fmp_utils import (
    ratings_snapshot,
    technical_indicator,
    search_stock_news,
    articles,
    price_target_news,
    grades_news,
)
from signals.scoring import fetch_yfinance_stock_data, SkipSymbol, YFPricesMissingError


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


def _price_target_features(symbol: str) -> dict[str, float]:
    data = price_target_news(symbol, limit=5)
    latest_upside = 0.0
    if isinstance(data, list):
        for item in data:
            target = item.get("priceTarget")
            posted = item.get("priceWhenPosted")
            if not isinstance(target, (int, float)) or not isinstance(posted, (int, float)):
                continue
            latest_upside = ((target / posted) - 1.0) * 100.0
            break
    return {"upside_pct": latest_upside}


def _sentiment_features(items) -> dict[str, float]:
    bullish = 0
    bearish = 0
    for item in items or []:
        title = (item.get("title") or "").lower()
        if any(k in title for k in BULLISH_KEYWORDS):
            bullish += 1
        if any(k in title for k in BEARISH_KEYWORDS):
            bearish += 1
    return {
        "bullish_count": float(bullish),
        "bearish_count": float(bearish),
    }


def _grade_value(symbol: str) -> float:
    data = grades_news(symbol, limit=1)
    if not data:
        return 0.0
    grade = (data[0].get("newGrade") or "").lower()
    return float(_GRADE_MAPPING.get(grade, 0.0))


def get_symbol_features(symbol: str) -> dict[str, float]:
    """Return a flat numeric feature dict for ``symbol`` from all providers."""
    features: dict[str, float | int | None] = {}

    quiver_features = quiver_utils.fetch_quiver_signals(symbol)
    features.update(quiver_features or {})

    features["fmp_grade_score"] = _grade_value(symbol)

    rating = ratings_snapshot(symbol)
    if rating:
        features["fmp_rating_overall_score"] = rating[0].get("overallScore")

    rsi_data = technical_indicator("rsi", symbol, 10, "1day")
    if rsi_data:
        features["fmp_rsi_value"] = rsi_data[0].get("rsi")

    news_items = search_stock_news(symbol, limit=5)
    news_features = _sentiment_features(news_items)
    features["fmp_news_bullish_count"] = news_features["bullish_count"]
    features["fmp_news_bearish_count"] = news_features["bearish_count"]

    art_items = [a for a in articles(limit=20) if symbol.upper() in (a.get("tickers") or "")]
    art_features = _sentiment_features(art_items)
    features["fmp_articles_bullish_count"] = art_features["bullish_count"]
    features["fmp_articles_bearish_count"] = art_features["bearish_count"]

    pt_features = _price_target_features(symbol)
    features["fmp_price_target_upside_pct"] = pt_features["upside_pct"]

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
