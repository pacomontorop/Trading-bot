import os
import pandas as pd

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

import core.executor as executor


def test_update_risk_limits(monkeypatch):
    # Simulate high risk environment
    monkeypatch.setattr(executor, "calculate_var", lambda window=30, confidence=0.95: 0.06)
    monkeypatch.setattr(executor, "get_max_drawdown", lambda window=30: -12)
    executor.MAX_POSITION_PCT = 0.10
    executor.DAILY_INVESTMENT_LIMIT_PCT = 0.50
    executor.update_risk_limits()
    assert executor.MAX_POSITION_PCT == 0.05
    assert executor.DAILY_INVESTMENT_LIMIT_PCT == 0.25

    # Simulate low risk environment
    monkeypatch.setattr(executor, "calculate_var", lambda window=30, confidence=0.95: 0.02)
    monkeypatch.setattr(executor, "get_max_drawdown", lambda window=30: -2)
    executor.update_risk_limits()
    assert executor.MAX_POSITION_PCT == 0.10
    assert executor.DAILY_INVESTMENT_LIMIT_PCT == 0.50


def test_adaptive_trail_price_window(monkeypatch):
    data = pd.DataFrame({
        "High": [10, 11, 12],
        "Low": [8, 9, 9],
        "Close": [9, 10, 11],
    })
    from types import SimpleNamespace
    monkeypatch.setattr(
        executor,
        "yf",
        SimpleNamespace(download=lambda *args, **kwargs: data),
        raising=False,
    )
    price = executor.get_adaptive_trail_price("TST", window=2)
    assert price == 0.55
