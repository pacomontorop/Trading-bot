import os
from typing import Any, Dict, Optional

import requests

BASE_URL = "https://api.tiingo.com"
TOKEN = os.getenv("TIINGO_API")


def _auth_headers() -> Dict[str, str]:
    if not TOKEN:
        raise RuntimeError("TIINGO_API environment variable not set")
    return {"Content-Type": "application/json", "Authorization": f"Token {TOKEN}"}


def get_daily_prices(ticker: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Any:
    """Fetch historical daily prices for ``ticker`` from Tiingo.

    Parameters
    ----------
    ticker:
        The asset symbol, e.g. ``AAPL``.
    start_date, end_date:
        Optional ISO formatted dates (YYYY-MM-DD).
    """
    url = f"{BASE_URL}/tiingo/daily/{ticker}/prices"
    params: Dict[str, str] = {}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    response = requests.get(url, headers=_auth_headers(), params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def get_iex_quote(ticker: str) -> Any:
    """Return the latest IEX quote for ``ticker``.

    Example response contains fields like ``last``, ``bidPrice``, ``askPrice``
    and volume information. See Tiingo's documentation for details.
    """
    url = f"{BASE_URL}/iex/{ticker}"
    response = requests.get(url, headers=_auth_headers(), timeout=10)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data[0] if data else {}
    return data
