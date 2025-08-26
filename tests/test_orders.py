import os
import sys

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

# Ensure project root is on sys.path for module resolution
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.orders import resolve_time_in_force, enforce_min_price_increment, submit_bracket_order
from broker import alpaca
from unittest import mock


def test_resolve_time_in_force_crypto_fractional():
    assert resolve_time_in_force(0.5, asset_class="crypto") == "gtc"


def test_resolve_time_in_force_equity_fractional():
    assert resolve_time_in_force(0.5, asset_class="us_equity") == "day"


def test_enforce_min_price_increment():
    assert enforce_min_price_increment(0.123456) == 0.1235
    assert enforce_min_price_increment(5.6789) == 5.68


def test_submit_bracket_order(monkeypatch):
    called = {}

    def fake_submit_order(**kwargs):
        called.update(kwargs)
        return mock.MagicMock(id="1")

    monkeypatch.setattr(alpaca.api, "submit_order", fake_submit_order)

    submit_bracket_order(
        symbol="AAPL",
        qty=1,
        side="buy",
        take_profit=150.1234,
        stop_loss=140.1234,
        limit_price=145.1234,
    )

    assert called["order_class"] == "bracket"
    assert called["take_profit"]["limit_price"] == 150.12
    assert called["stop_loss"]["stop_price"] == 140.12
