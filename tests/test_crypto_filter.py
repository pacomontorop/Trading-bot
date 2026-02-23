"""Tests for crypto-pair detection and filtering in utils/symbols.py."""

from __future__ import annotations

import pytest
from utils.symbols import is_crypto, detect_asset_class
from utils.universe import load_universe
import csv
import tempfile
import os


# ---------------------------------------------------------------------------
# is_crypto()
# ---------------------------------------------------------------------------

CRYPTO_PAIRS = [
    "BTCUSD",
    "ETHUSD",
    "DOGEUSD",
    "SHIBUSD",
    "SOLUSD",
    "AVAXUSD",
    "LTCUSD",
    "LINKUSD",
    "MATICUSD",
    "UNIUSD",
    "AAVEUSD",
    "BCHUSD",
    "ALGOUSD",
    "XTZUSD",
    "MKRUSD",
    "GRTUSD",
    "BATUSD",
    "YFIUSD",
    "SUSHIUSD",
    "BTCUSDT",
    "ETHUSDT",
    "BTCBUSD",
]

EQUITY_TICKERS = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "TSLA",
    "KO",
    "JNJ",
    "IBM",
    "IUSB",   # bond ETF containing "USB"
    "SUSC",   # bond ETF
    "BNDW",   # bond ETF
    "DE",     # Deere & Company (2-char ticker)
    "F",      # Ford (1-char ticker)
    "BRK.B",  # Berkshire Hathaway B
    "BRK-B",  # Berkshire alt format
    "SPY",
    "QQQ",
    "GLD",    # gold ETF (not crypto)
    "SLV",    # silver ETF
]


@pytest.mark.parametrize("symbol", CRYPTO_PAIRS)
def test_is_crypto_true(symbol: str) -> None:
    assert is_crypto(symbol), f"Expected {symbol!r} to be detected as crypto"


@pytest.mark.parametrize("symbol", EQUITY_TICKERS)
def test_is_crypto_false(symbol: str) -> None:
    assert not is_crypto(symbol), f"Expected {symbol!r} NOT to be detected as crypto"


def test_is_crypto_empty_string() -> None:
    assert not is_crypto("")


def test_is_crypto_none_like() -> None:
    # Guard against accidental None passed as symbol
    assert not is_crypto(None)  # type: ignore[arg-type]


def test_is_crypto_lowercase() -> None:
    # Must be case-insensitive
    assert is_crypto("btcusd")
    assert is_crypto("Dogeusd")


# ---------------------------------------------------------------------------
# detect_asset_class()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol", CRYPTO_PAIRS)
def test_detect_asset_class_crypto(symbol: str) -> None:
    assert detect_asset_class(symbol) == "crypto"


@pytest.mark.parametrize("symbol", EQUITY_TICKERS)
def test_detect_asset_class_equity(symbol: str) -> None:
    result = detect_asset_class(symbol)
    assert result in ("equity", "preferred"), (
        f"Expected equity/preferred for {symbol!r}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# load_universe() excludes crypto rows
# ---------------------------------------------------------------------------

def _make_symbols_csv(rows: list[dict], path: str) -> None:
    fieldnames = ["Symbol", "Name", "Exchange", "Tradable", "Shortable", "Marginable"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_load_universe_excludes_crypto(tmp_path) -> None:
    csv_path = str(tmp_path / "symbols.csv")
    _make_symbols_csv(
        [
            {"Symbol": "AAPL", "Name": "Apple", "Exchange": "NASDAQ",
             "Tradable": "True", "Shortable": "True", "Marginable": "True"},
            {"Symbol": "BTCUSD", "Name": "Bitcoin/USD", "Exchange": "CRYPTO",
             "Tradable": "True", "Shortable": "False", "Marginable": "False"},
            {"Symbol": "DOGEUSD", "Name": "Dogecoin/USD", "Exchange": "CRYPTO",
             "Tradable": "True", "Shortable": "False", "Marginable": "False"},
            {"Symbol": "MSFT", "Name": "Microsoft", "Exchange": "NASDAQ",
             "Tradable": "True", "Shortable": "True", "Marginable": "True"},
            {"Symbol": "ETHUSD", "Name": "Ethereum/USD", "Exchange": "CRYPTO",
             "Tradable": "True", "Shortable": "False", "Marginable": "False"},
        ],
        csv_path,
    )

    universe = load_universe(csv_path)
    symbols = [entry["symbol"] for entry in universe]

    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "BTCUSD" not in symbols, "BTCUSD (crypto) must be excluded from universe"
    assert "DOGEUSD" not in symbols, "DOGEUSD (crypto) must be excluded from universe"
    assert "ETHUSD" not in symbols, "ETHUSD (crypto) must be excluded from universe"
