import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils.state import mark_evaluated
import signals.gates as gates


def setup_env(monkeypatch):
    monkeypatch.setattr(gates, "fetch_quiver_signals", lambda s: {"insider_buy_more_than_sell": {"age": 1}})
    monkeypatch.setattr(gates, "get_current_price", lambda s: 10.0)
    monkeypatch.setattr(gates, "fetch_yfinance_stock_data", lambda s: (600_000_000, 600_000, None, None, None, None))
    monkeypatch.setattr(gates, "is_market_open", lambda: True)
    monkeypatch.setattr(gates, "is_blacklisted_recent_loser", lambda s: False)


def test_dedupe(monkeypatch):
    setup_env(monkeypatch)
    mark_evaluated("AAPL")
    ok, reasons = gates.passes_long_gate("AAPL")
    assert not ok
    assert "duplicate" in reasons
