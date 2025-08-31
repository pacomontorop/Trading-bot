import yfinance as yf
from datetime import datetime, timedelta
from config import STRATEGY_VER
import os
import yaml

_POLICY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "policy.yaml")
with open(_POLICY_PATH, "r", encoding="utf-8") as _f:
    _policy = yaml.safe_load(_f)
SCORE_CFG = _policy.get("score", {})
STRONG_RECENCY_HOURS = float(SCORE_CFG.get("strong_recency_hours", 48))

_CACHE_TTL = timedelta(minutes=5)
_stock_cache = {}


def _normalize_0_100(x: float) -> int:
    """Coerce ``x`` into an integer within the [0, 100] range."""
    try:
        return max(0, min(100, int(round(x))))
    except Exception:
        return 0


def fetch_yfinance_stock_data(symbol, verbose: bool = False):
    now = datetime.utcnow()
    cached = _stock_cache.get(symbol)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        return cached["data"]
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        market_cap = info.get("marketCap")
        volume = info.get("volume")
        hist = ticker.history(period="21d", interval="1d")
        weekly_change = None
        if len(hist) >= 2:
            weekly_change = ((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0]) * 100
        trend_positive = hist['Close'].iloc[-1] > hist['Close'].iloc[0] if len(hist) >= 2 else None
        price_change_24h = abs((hist['Close'].iloc[-1] - hist['Close'].iloc[-2]) / hist['Close'].iloc[-2]) * 100 if len(hist) >= 2 else None
        volume_7d_avg = hist['Volume'].mean() if not hist['Volume'].isna().all() else None
        data = (market_cap, volume, weekly_change, trend_positive, price_change_24h, volume_7d_avg)
        _stock_cache[symbol] = {"data": data, "ts": now}
        return data
    except Exception:
        return None, None, None, None, None, None


def score_long_signal(symbol: str, market_data: dict) -> dict:
    """Return a normalized 0-100 score for ``symbol`` based on ``market_data``."""
    components = {}
    score = 0
    quiver = market_data.get("quiver", {})
    quiver_score = 0
    strong_count = 0
    quiver_recent = False

    def _decay(base, days):
        return base if days <= 3 else max(5, int(base * (0.9 ** days)))

    if "insiders" in quiver:
        quiver_score += _decay(30, quiver["insiders"])
        strong_count += 1
        if quiver["insiders"] <= 3:
            quiver_recent = True
    if "gov_contract" in quiver:
        quiver_score += _decay(25, quiver["gov_contract"])
        strong_count += 1
        if quiver["gov_contract"] <= 3:
            quiver_recent = True
    if "patent_momentum" in quiver:
        quiver_score += _decay(15, quiver["patent_momentum"])
        strong_count += 1
        if quiver["patent_momentum"] <= 3:
            quiver_recent = True
    if "sec13f_activity" in quiver:
        quiver_score += _decay(5, quiver["sec13f_activity"])
    if "sec13f_changes" in quiver:
        quiver_score += _decay(8, quiver["sec13f_changes"])
    if "house" in quiver:
        quiver_score += _decay(10, quiver["house"])
    quiver_score += min(3, quiver.get("wsb", 0))
    quiver_score += min(3, quiver.get("twitter", 0))
    components["quiver"] = quiver_score
    if strong_count >= 2:
        components["quiver_double_strong"] = True

    fmp = market_data.get("fmp", {})
    rs = fmp.get("ratings_snapshot", 0)
    components["ratings_snapshot"] = min(max(rs, 0), 10)
    score += components["ratings_snapshot"]
    rsi = fmp.get("rsi")
    if rsi is not None:
        rsi_score = 5 if rsi < 30 else -5 if rsi > 70 else 0
        components["rsi"] = rsi_score
        score += rsi_score
    news = fmp.get("news_polarity", 0)
    components["news_polarity"] = max(min(news, 5), -5)
    score += components["news_polarity"]
    if score > 20:
        score = 20
    score += quiver_score

    penalties = 0
    atr_ratio = market_data.get("atr_ratio")
    if atr_ratio and atr_ratio > 0:
        penalties += 10
    gap = market_data.get("gap", 0)
    if gap and gap > 0:
        penalties += 5
    macro = market_data.get("macro_vix", 0)
    if macro and macro > 0:
        penalties += 5
    score -= penalties
    components["penalties"] = -penalties

    score = _normalize_0_100(score)
    return {"score": score, "components": components, "quiver_recent": quiver_recent}
