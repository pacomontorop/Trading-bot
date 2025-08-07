# fmp_utils.py
"""Helper functions for Financial Modeling Prep (FMP) API.
These functions serve as backups when primary data sources fail."""
import os
import time
import requests
from signals.quiver_throttler import throttled_request

BASE_URL = "https://financialmodelingprep.com/stable"

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
            return None
    return None

def stock_screener(**params):
    """Wrapper for the Stock Screener API."""
    return _get("company-screener", params)

def shares_float(symbol: str):
    """Get company share float and liquidity information."""
    return _get("shares-float", {"symbol": symbol})

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

def grades_news(symbol: str, page: int = 0, limit: int = 1):
    params = {"symbol": symbol, "page": page, "limit": limit}
    return _get("grades-news", params)
