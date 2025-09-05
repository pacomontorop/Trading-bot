import os, sys, types
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import core.executor as executor
import core.order_utils as order_utils


class DummyOrder:
    def __init__(self):
        self.id = "1"
        self.status = "filled"


def test_idempotent_order(monkeypatch):
    # Patch environment to avoid real API calls
    monkeypatch.setattr(order_utils, "alpaca_order_exists", lambda cid: True)
    monkeypatch.setattr(executor, "alpaca_order_exists", lambda cid: True)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(executor, "is_symbol_approved", lambda s, score, cfg: True)
    monkeypatch.setattr(executor, "is_position_open", lambda s: False)
    monkeypatch.setattr(executor, "get_current_price", lambda s: 10.0)
    monkeypatch.setattr(executor.api, "get_account", lambda: types.SimpleNamespace(equity="10000", buying_power="10000", cash="10000"))
    monkeypatch.setattr(executor.api, "get_asset", lambda s: types.SimpleNamespace(tradable=True, fractionable=True))
    submit_called = {}
    def fake_submit_order(**kwargs):
        submit_called['called'] = True
        return DummyOrder()
    monkeypatch.setattr(executor.api, "submit_order", fake_submit_order)
    sizing = {"notional": 1000, "shares": 100, "stop_distance": 1}
    res = executor.place_order_with_trailing_stop("AAPL", sizing)
    assert res is False
    assert 'called' not in submit_called
