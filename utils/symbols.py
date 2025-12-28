"""Helpers for routing symbols by asset class and normalizing tickers."""

from __future__ import annotations


def detect_asset_class(symbol: str) -> str:
    """Return an asset class identifier for ``symbol``.

    Asset classes currently recognized: ``equity`` and ``preferred``.
    """

    if not symbol:
        return "equity"
    s = symbol.upper()
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
