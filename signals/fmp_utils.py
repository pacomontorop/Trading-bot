# fmp_utils.py
"""Helper functions for Financial Modeling Prep (FMP) API.
These functions serve as backups when primary data sources fail."""
import os
import time
import requests
from signals.quiver_throttler import throttled_request

BASE_URL = "https://financialmodelingprep.com/stable"

# Default timeout (seconds) for FMP HTTP requests. Increase if API is slow.
REQUEST_TIMEOUT = float(os.getenv("FMP_TIMEOUT", 30))

# Minimum seconds between grade-news requests to avoid hitting FMP limits.
GRADES_NEWS_MIN_INTERVAL = float(os.getenv("FMP_GRADES_NEWS_DELAY", 15))
_last_grades_news_call = 0.0

def _get(endpoint: str, params: dict | None = None, max_retries: int = 3):
    key = os.getenv("FMP_API_KEY")
    if params is None:
        params = {}
    if key:
        params["apikey"] = key
    for attempt in range(max_retries):
        try:
            resp = throttled_request(
                requests.get,
                f"{BASE_URL}/{endpoint}",
                params=params,
                timeout=REQUEST_TIMEOUT,
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
    params = {"symbol": symbol, "page": page, "limit": limit}
    # The FMP Stock Grade News endpoint expects the symbol as a query parameter.
    return _get("grades-news", params)


# --- Additional FMP helpers for deeper integration ---

def as_reported_income_statement(symbol: str, period: str = "annual", limit: int = 5):
    params = {"symbol": symbol, "period": period, "limit": limit}
    return _get("income-statement-as-reported", params)


def as_reported_balance_sheet(symbol: str, period: str = "annual", limit: int = 5):
    params = {"symbol": symbol, "period": period, "limit": limit}
    return _get("balance-sheet-statement-as-reported", params)


def as_reported_cash_flow(symbol: str, period: str = "annual", limit: int = 5):
    params = {"symbol": symbol, "period": period, "limit": limit}
    return _get("cash-flow-statement-as-reported", params)


def financial_statement_full_as_reported(symbol: str, period: str = "annual", limit: int = 5):
    params = {"symbol": symbol, "period": period, "limit": limit}
    return _get("financial-statement-full-as-reported", params)


def ratings_snapshot(symbol: str, limit: int = 1):
    params = {"symbol": symbol, "limit": limit}
    return _get("ratings-snapshot", params)


def technical_indicator(
    indicator: str, symbol: str, period_length: int = 10, timeframe: str = "1day", **params
):
    params.update(
        {
            "symbol": symbol,
            "periodLength": period_length,
            "timeframe": timeframe,
        }
    )
    return _get(f"technical-indicators/{indicator}", params)


def articles(page: int = 0, limit: int = 20):
    params = {"page": page, "limit": limit}
    return _get("fmp-articles", params)


def search_stock_news(
    symbols: str, from_date: str | None = None, to_date: str | None = None, page: int = 0, limit: int = 20
):
    params = {"symbols": symbols, "page": page, "limit": limit}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    return _get("news/stock", params)


def treasury_rates(from_date: str, to_date: str):
    params = {"from": from_date, "to": to_date}
    return _get("treasury-rates", params)


def sec_filings_latest(from_date: str, to_date: str, page: int = 0, limit: int = 100):
    params = {"from": from_date, "to": to_date, "page": page, "limit": limit}
    return _get("sec-filings-financials", params)


def sec_filings_8k_latest(from_date: str, to_date: str, page: int = 0, limit: int = 100):
    params = {"from": from_date, "to": to_date, "page": page, "limit": limit}
    return _get("sec-filings-8k", params)


def sec_filings_by_form(
    form_type: str, from_date: str, to_date: str, page: int = 0, limit: int = 100
):
    params = {
        "formType": form_type,
        "from": from_date,
        "to": to_date,
        "page": page,
        "limit": limit,
    }
    return _get("sec-filings-search/form-type", params)


def sec_filings_by_symbol(
    symbol: str, from_date: str, to_date: str, page: int = 0, limit: int = 100
):
    params = {"symbol": symbol, "from": from_date, "to": to_date, "page": page, "limit": limit}
    return _get("sec-filings-search/symbol", params)


def sec_company_profile(symbol: str):
    return _get("sec-profile", {"symbol": symbol})
