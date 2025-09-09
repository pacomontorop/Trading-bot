import types

import core.executor as executor
from utils.state import StateManager


def test_timeout_then_pending(monkeypatch):
    StateManager.clear()

    def fake_order_exists(client_order_id):
        return False

    def fake_submit_order(**kwargs):
        return True, "1"

    def fake_wait(coid, timeout_sec, initial_delay, backoff_factor, max_delay):
        return types.SimpleNamespace(state="new")

    monkeypatch.setattr(executor.broker, "order_exists", fake_order_exists)
    monkeypatch.setattr(executor.broker, "submit_order", fake_submit_order)
    monkeypatch.setattr(executor, "_wait_for_fill_or_timeout", fake_wait)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    res = executor.place_order_with_trailing_stop("AAPL", "buy", 1, "market", None, {})
    assert res is True
    assert StateManager.get_open_orders() == {"AAPL": executor.make_client_order_id("AAPL", "buy")}
    assert StateManager.get_open_positions() == {}
    assert StateManager.get_executed_symbols() == set()
