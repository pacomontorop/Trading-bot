# fmp_utils.py
"""Helper functions for Financial Modeling Prep (FMP) API.
These functions serve as backups when primary data sources fail."""
import os
import requests

BASE_URL = "https://financialmodelingprep.com/stable"

def _get(endpoint: str, params: dict | None = None):
    key = os.getenv("FMP_API_KEY")
    if params is None:
        params = {}
    if key:
        params["apikey"] = key
    try:
        resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"⚠️ FMP request failed ({endpoint}): {e}")
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
