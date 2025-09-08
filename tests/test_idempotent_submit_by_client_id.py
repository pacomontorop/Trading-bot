import types

import core.executor as executor
from utils.state import StateManager


def test_idempotent_submit_by_client_id(monkeypatch):
    StateManager.clear()

    exists_calls = iter([False, True])
    submit_called = []
    reconcile_called = []

    def fake_order_exists(client_order_id):
        return next(exists_calls)

    def fake_submit_order(**kwargs):
        submit_called.append(kwargs)
        status = types.SimpleNamespace(state="filled", filled_qty=1, filled_avg_price=10)
        return True, "1"

    def fake_wait(coid, timeout_sec, initial_delay, backoff_factor, max_delay):
        return types.SimpleNamespace(state="filled", filled_qty=1, filled_avg_price=10)

    def fake_reconcile(symbol, coid, cfg):
        reconcile_called.append((symbol, coid))
        return True

    monkeypatch.setattr(executor.broker, "order_exists", fake_order_exists)
    monkeypatch.setattr(executor.broker, "submit_order", fake_submit_order)
    monkeypatch.setattr(executor, "_wait_for_fill_or_timeout", fake_wait)
    monkeypatch.setattr(executor, "_reconcile_existing_order", fake_reconcile)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    executor.place_order_with_trailing_stop("AAPL", "buy", 1, "market", None, {})
    executor.place_order_with_trailing_stop("AAPL", "buy", 1, "market", None, {})

    assert len(submit_called) == 1
    assert len(reconcile_called) == 1
