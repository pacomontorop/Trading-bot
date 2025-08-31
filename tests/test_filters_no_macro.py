import sys

def test_is_symbol_approved_no_macro(monkeypatch):
    from signals import filters

    monkeypatch.setattr(filters, "volatility_penalty", lambda s: 0.0)
    monkeypatch.setattr(filters, "reddit_score", lambda s: 0.0)
    monkeypatch.setattr(filters, "is_approved_by_quiver", lambda s: True)
    monkeypatch.setattr(filters, "is_approved_by_finnhub_and_alphavantage", lambda s: False)
    monkeypatch.setattr(filters, "is_approved_by_fmp", lambda s: False)

    assert filters.is_symbol_approved("AAPL") is True
    assert "data.fred_client" not in sys.modules
