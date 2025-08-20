# fmp_signals.py
"""Derive simple bullish/bearish signals from FMP endpoints."""
from datetime import datetime
from typing import Optional, Dict

from signals.aggregator import WeightedSignalAggregator
from .fmp_utils import (
    ratings_snapshot,
    technical_indicator,
    search_stock_news,
    articles,
)

BULLISH_KEYWORDS = {"growth", "bullish", "beat", "surge"}
BEARISH_KEYWORDS = {"bearish", "decline", "drop", "weak"}


def _extract_sentiment(items) -> tuple[Optional[float], Optional[datetime]]:
    score = 0.0
    latest = None
    for item in items or []:
        title = (item.get("title") or "").lower()
        ts_str = item.get("publishedDate") or item.get("date")
        ts = None
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.split(" ")[0])
            except Exception:
                ts = None
        if any(k in title for k in BULLISH_KEYWORDS):
            score += 1
        if any(k in title for k in BEARISH_KEYWORDS):
            score -= 1
        if ts and (latest is None or ts > latest):
            latest = ts
    if score == 0:
        return None, latest
    return score, latest


def get_fmp_signal_score(symbol: str) -> Optional[Dict]:
    """Return a composite FMP-based score with timestamp."""
    components: Dict[str, Dict] = {}
    now = datetime.utcnow()

    # Rating snapshot
    rating = ratings_snapshot(symbol)
    if rating:
        overall = rating[0].get("overallScore")
        if isinstance(overall, (int, float)):
            components["rating"] = {"score": overall / 5.0, "timestamp": now}

    # RSI indicator
    rsi_data = technical_indicator("rsi", symbol, 10, "1day")
    if rsi_data:
        rsi = rsi_data[0].get("rsi")
        if isinstance(rsi, (int, float)):
            if rsi < 30:
                val = 1.0
            elif rsi > 70:
                val = -1.0
            else:
                val = 0.0
            components["rsi"] = {"score": val, "timestamp": now}

    # News sentiment
    news_items = search_stock_news(symbol, limit=5)
    news_score, news_ts = _extract_sentiment(news_items)
    if news_score is not None:
        components["news"] = {"score": news_score, "timestamp": news_ts or now}

    # Articles sentiment
    art_items = [a for a in articles(limit=20) if symbol.upper() in (a.get("tickers") or "")]
    art_score, art_ts = _extract_sentiment(art_items)
    if art_score is not None:
        components["articles"] = {"score": art_score, "timestamp": art_ts or now}

    if not components:
        return None

    agg = WeightedSignalAggregator({"rating": 2, "rsi": 1, "news": 1, "articles": 0.5})
    score = agg.combine(components)
    ts = max(v["timestamp"] for v in components.values() if v.get("timestamp"))
    return {"score": score, "timestamp": ts}

