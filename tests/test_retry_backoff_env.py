import importlib
import types
import os
import alpaca_trade_api as tradeapi


def test_retry_backoff_env(monkeypatch):
    class DummyREST:
        def __init__(self, *a, **k):
            self._session = types.SimpleNamespace(mount=lambda *a, **k: None)

    monkeypatch.setenv("APCA_API_KEY_ID", "key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "secret")
    monkeypatch.setenv("APCA_RETRY_TOTAL", "5")
    monkeypatch.setenv("APCA_BACKOFF_FACTOR", "0.5")
    monkeypatch.setattr(tradeapi, "REST", DummyREST)

    import broker.alpaca as alpaca
    alpaca = importlib.reload(alpaca)

    assert alpaca.retry.total == 5
    assert alpaca.retry.backoff_factor == 0.5
