import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import signals.filters as filters
import signals.gates as gates


def test_approval_does_not_call_gate(monkeypatch):
    monkeypatch.setattr(filters, "macro_score", lambda: 0.0)
    monkeypatch.setattr(filters, "volatility_penalty", lambda s: 0.0)
    monkeypatch.setattr(filters, "reddit_score", lambda s: 0.0)
    monkeypatch.setattr(filters, "is_approved_by_finnhub_and_alphavantage", lambda s: True)
    monkeypatch.setattr(filters, "is_approved_by_fmp", lambda s: False)
    monkeypatch.setattr(gates, "_has_strong_recent_quiver_signal", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("should not be called")))
    assert filters.is_symbol_approved("AAPL") is True
