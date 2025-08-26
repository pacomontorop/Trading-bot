"""Basic crypto signal generator using public CoinGecko data.

This is intentionally simple and network-light: it fetches the trending coins
from CoinGecko and returns those that are also tradable on Alpaca.  The caller
may layer additional sentiment filters (e.g. Reddit) externally.
"""

from typing import List, Tuple
import requests

from broker.alpaca import api


def get_crypto_signals(limit: int = 5) -> List[Tuple[str, int]]:
    """Return a list of ``(symbol, score)`` tuples.

    The current implementation assigns a flat score of 80 to each trending
    asset that is tradable on Alpaca.  This stub exists so the crypto worker can
    run without relying on complex external services.  In production you may
    extend it with Reddit sentiment or more advanced metrics.
    """
    try:
        data = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10).json()
    except Exception:
        return []

    results: List[Tuple[str, int]] = []
    for item in data.get("coins", []):
        sym = item.get("item", {}).get("symbol", "").upper()
        alpaca_symbol = f"{sym}USD"
        try:
            asset = api.get_asset(alpaca_symbol)
            if asset.tradable:
                results.append((alpaca_symbol, 80))
        except Exception:
            continue
        if len(results) >= limit:
            break
    return results
