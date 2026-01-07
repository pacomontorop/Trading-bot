"""Universe loading helpers."""

from __future__ import annotations

import csv
from typing import Any

from utils.symbols import detect_asset_class, normalize_ticker


def parse_bool(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def load_universe(path: str) -> list[dict[str, Any]]:
    """Load the trading universe CSV using DictReader."""
    universe: list[dict[str, Any]] = []
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                symbol = (row.get("Symbol") or "").strip().upper()
                if not symbol:
                    continue
                if detect_asset_class(symbol) != "equity":
                    continue
                tradable = parse_bool(row.get("Tradable"))
                if not tradable:
                    continue
                entry = {
                    "symbol": symbol,
                    "name": (row.get("Name") or "").strip(),
                    "exchange": (row.get("Exchange") or "").strip().upper(),
                    "tradable": tradable,
                    "shortable": parse_bool(row.get("Shortable")),
                    "marginable": parse_bool(row.get("Marginable")),
                    "ticker_map": normalize_ticker(symbol),
                }
                universe.append(entry)
    except Exception:
        return []
    return universe
