"""Helpers for routing symbols by asset class and normalizing tickers."""

from __future__ import annotations

import re

# Matches crypto trading pairs: 2-10 char base + known quote currency suffix.
# Examples: BTCUSD, ETHUSD, DOGEUSD, SHIBUSD, SOLUSD, AVAXUSD, BTCUSDT.
# Equity tickers (AAPL, IUSB, BNDW, etc.) do not match this pattern.
_CRYPTO_SUFFIX_RE = re.compile(
    r"^[A-Z0-9]{2,10}(USD|USDT|USDC|BUSD|BTC|ETH|EUR|GBP|PERP)$"
)


def is_crypto(symbol: str) -> bool:
    """Return True if *symbol* looks like a crypto trading pair.

    Detects pairs such as BTCUSD, DOGEUSD, SHIBUSD, ETHUSD, SOLUSD, etc.
    Returns False for normal equity tickers and preferred-stock suffixes.
    """
    if not symbol:
        return False
    s = symbol.upper().strip()
    # Equity tickers with dots or hyphens (e.g. BRK.B, BRK-B) are never crypto.
    if "." in s or "-" in s:
        return False
    return bool(_CRYPTO_SUFFIX_RE.match(s))


def detect_asset_class(symbol: str) -> str:
    """Return an asset class identifier for ``symbol``.

    Asset classes currently recognized: ``equity``, ``preferred``, ``crypto``.
    """

    if not symbol:
        return "equity"
    s = symbol.upper()
    if is_crypto(s):
        return "crypto"
    if ".PR" in s or ".PRA" in s or ".PRB" in s or ".PRC" in s:
        return "preferred"
    if "." in s:
        parts = s.split(".")
        if parts[-1].startswith("PR"):
            return "preferred"
    return "equity"


def normalize_for_yahoo(symbol: str) -> str:
    """Return a Yahoo Finance compatible ticker for ``symbol``."""

    if not symbol:
        return symbol
    s = symbol.upper()
    if ".PR" in s:
        base, pr = s.split(".PR", 1)
        if pr:
            return f"{base}-P{pr[0]}"
    return s


def normalize_ticker(symbol: str) -> dict[str, str]:
    """Return canonical/provider-specific ticker mappings for ``symbol``."""

    canonical = (symbol or "").strip().upper()
    if not canonical:
        return {"canonical": "", "yahoo": "", "quiver": ""}

    if "." in canonical:
        yahoo = canonical.replace(".", "-")
        quiver = canonical
    else:
        yahoo = canonical
        quiver = canonical

    return {
        "canonical": canonical,
        "yahoo": yahoo,
        "quiver": quiver,
    }
