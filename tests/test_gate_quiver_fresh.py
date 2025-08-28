import os, sys, importlib
from datetime import date
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import signals.gates as gates
from utils import state


def setup(monkeypatch, age):
    importlib.reload(state)
    try:
        os.remove(os.path.join("data", f"state_{date.today():%Y%m%d}.json"))
    except FileNotFoundError:
        pass
    monkeypatch.setattr(gates, "fetch_quiver_signals", lambda s: {"insider_buy_more_than_sell": {"age": age}})
    monkeypatch.setattr(gates, "get_current_price", lambda s: 10.0)
    monkeypatch.setattr(gates, "fetch_yfinance_stock_data", lambda s: (600_000_000, 600_000, None, None, None, None))
    monkeypatch.setattr(gates, "is_market_open", lambda: True)
    monkeypatch.setattr(gates, "is_blacklisted_recent_loser", lambda s: False)


def test_gate_quiver_recent(monkeypatch):
    setup(monkeypatch, 2)
    ok, details = gates.passes_long_gate("AAPL")
    assert ok
    assert details["quiver_strong"]


def test_gate_quiver_stale(monkeypatch):
    setup(monkeypatch, 5)
    ok, reasons = gates.passes_long_gate("AAPL")
    assert not ok
    assert "quiver" in reasons
