import types

import core.executor as executor
from utils.state import StateManager


def test_atomic_state_update_on_fill(monkeypatch):
    StateManager.clear()

    def fake_order_exists(client_order_id):
        return False

    def fake_submit_order(**kwargs):
        return True, "1"

    def fake_wait(coid, timeout_sec):
        return types.SimpleNamespace(state="filled", filled_qty=5, filled_avg_price=10)

    monkeypatch.setattr(executor.broker, "order_exists", fake_order_exists)
    monkeypatch.setattr(executor.broker, "submit_order", fake_submit_order)
    monkeypatch.setattr(executor, "_wait_for_fill_or_timeout", fake_wait)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(executor, "reconcile_after_fill", lambda *a, **k: None)
    monkeypatch.setattr(executor.api, "get_position", lambda symbol: types.SimpleNamespace(qty=0), raising=False)
    monkeypatch.setattr(executor, "get_current_price", lambda symbol: 100)
    monkeypatch.setattr(executor.broker, "list_open_orders_today", lambda: [])
    executor._recent_intents.clear()
    executor._intent_by_coid.clear()

    res = executor.place_order_with_trailing_stop("AAPL", "buy", 5, "market", None, {})
    assert res is True
    assert StateManager.get_open_orders() == {}
    positions = StateManager.get_open_positions()
    assert positions["AAPL"]["qty"] == 5
    assert "AAPL" in StateManager.get_executed_symbols()
