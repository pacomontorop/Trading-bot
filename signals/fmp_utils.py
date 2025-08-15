# fmp_utils.py
"""Helper functions for Financial Modeling Prep (FMP) API.
These functions serve as backups when primary data sources fail."""
import os
import time
import requests
from signals.quiver_throttler import throttled_request

BASE_URL = "https://financialmodelingprep.com/stable"

# Minimum seconds between grade-news requests to avoid hitting FMP limits.
GRADES_NEWS_MIN_INTERVAL = float(os.getenv("FMP_GRADES_NEWS_DELAY", 15))
GRADES_CACHE_TTL = float(os.getenv("FMP_GRADES_CACHE_TTL", 86400))
_last_grades_news_call = 0.0
_grades_cache: dict[str, tuple[float | None, float]] = {}

# Map textual grades to numeric scores for weighting/thresholding
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

def _get(endpoint: str, params: dict | None = None, max_retries: int = 3):
    key = os.getenv("FMP_API_KEY")
    if params is None:
        params = {}
    if key:
        params["apikey"] = key
    for attempt in range(max_retries):
        try:
            resp = throttled_request(
                requests.get, f"{BASE_URL}/{endpoint}", params=params, timeout=10
            )
            if resp.status_code == 429:
                wait = 2 ** attempt
                print(
                    f"⚠️ FMP rate limit hit ({endpoint}). "
                    f"Retrying in {wait}s..."
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"⚠️ FMP request failed ({endpoint}): {e}")
            if attempt == max_retries - 1:
                return None
            time.sleep(2 ** attempt)
    return None

def stock_screener(**params):
    """Wrapper for the Stock Screener API."""
    return _get("company-screener", params)

def shares_float(symbol: str):
    """Get company share float and liquidity information."""
    return _get("shares-float", {"symbol": symbol})

def company_profile(symbol: str):
    """Fetch basic company profile information."""
    return _get(f"profile/{symbol}")

def quote(symbol: str):
    """Return the latest market quote for ``symbol``."""
    return _get(f"quote/{symbol}")

def financial_ratios(symbol: str, period: str = "annual", limit: int = 5):
    """Retrieve financial ratios such as PE and debt/equity."""
    params = {"period": period, "limit": limit}
    return _get(f"ratios/{symbol}", params)

def key_metrics(symbol: str, period: str = "annual", limit: int = 5):
    """Return key metrics like revenue per share."""
    params = {"period": period, "limit": limit}
    return _get(f"key-metrics/{symbol}", params)

def cot_report(symbol: str, from_date: str | None = None, to_date: str | None = None):
    params = {"symbol": symbol}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    return _get("commitment-of-traders-report", params)

def cot_analysis(symbol: str, from_date: str | None = None, to_date: str | None = None):
    params = {"symbol": symbol}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    return _get("commitment-of-traders-analysis", params)


def price_target_news(symbol: str, page: int = 0, limit: int = 10):
    """Return recent analyst price target news for ``symbol``."""
    params = {"symbol": symbol, "page": page, "limit": limit}
    return _get("price-target-news", params)

def grades_news(symbol: str, page: int = 0, limit: int = 1):
    """Fetch latest analyst grade news with throttling to respect rate limits."""
    global _last_grades_news_call
    now = time.time()
    elapsed = now - _last_grades_news_call
    if elapsed < GRADES_NEWS_MIN_INTERVAL:
        time.sleep(GRADES_NEWS_MIN_INTERVAL - elapsed)
    _last_grades_news_call = time.time()
    params = {"page": page, "limit": limit}
    # The FMP endpoint for analyst grade news expects the symbol in the path.
    # Using query parameters results in a 404 from the API.
    return _get(f"grade/{symbol}", params)


def get_fmp_grade_score(symbol: str) -> float | None:
    """Return numeric grade score for ``symbol`` with simple caching.

    Positive numbers indicate bullish grades, negatives bearish. Results are
    cached to avoid hitting FMP's rate limits when the function is called
    repeatedly during scans.
    """
    now = time.time()
    cached = _grades_cache.get(symbol)
    if cached and now - cached[1] < GRADES_CACHE_TTL:
        return cached[0]

    data = grades_news(symbol, limit=1)
    score: float | None = None
    if data:
        grade = (data[0].get("newGrade") or "").lower()
        score = _GRADE_MAPPING.get(grade)
    _grades_cache[symbol] = (score, now)
    return score
