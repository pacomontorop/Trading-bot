import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
os.environ.setdefault("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from signals import gates


def test_gate_liquidity_enforced(monkeypatch):
    monkeypatch.setattr(gates, "already_evaluated_today", lambda s: False)
    monkeypatch.setattr(gates, "already_executed_today", lambda s: False)
    monkeypatch.setattr(gates, "is_market_open", lambda: True)
    monkeypatch.setattr(gates, "is_blacklisted_recent_loser", lambda s: False)
    monkeypatch.setattr(gates, "_has_strong_recent_quiver_signal", lambda s, m: True)

    # Passing case
    monkeypatch.setattr(gates, "fetch_yfinance_stock_data", lambda s: (500e6, 500000, None, None, None, None))
    monkeypatch.setattr(gates, "get_current_price", lambda s: 5.0)
    ok, _ = gates.passes_long_gate("GOOD")
    assert ok

    # Failing case due to low liquidity and price
    monkeypatch.setattr(gates, "fetch_yfinance_stock_data", lambda s: (1e6, 1000, None, None, None, None))
    monkeypatch.setattr(gates, "get_current_price", lambda s: 1.0)
    ok, reasons = gates.passes_long_gate("BAD")
    assert not ok
    assert "liquidity" in reasons or "price" in reasons
