import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import signals.quiver_utils as q


def test_evaluate_quiver_signals_no_liquidity_call(monkeypatch):
    monkeypatch.setattr(
        "signals.scoring.fetch_yfinance_stock_data",
        lambda s: (_ for _ in ()).throw(RuntimeError("should not be called")),
        raising=True,
    )
    signals = {
        "insider_buy_more_than_sell": q.SignalResult(True, 0.5),
        "has_gov_contract": q.SignalResult(True, 0.5),
    }
    monkeypatch.setattr(q, "score_quiver_signals", lambda s: 10.0)
    monkeypatch.setattr(q, "has_recent_quiver_event", lambda s, days=2: True)
    monkeypatch.setattr(q, "log_event", lambda *a, **k: None)
    assert q.evaluate_quiver_signals(signals, "TEST") is True
