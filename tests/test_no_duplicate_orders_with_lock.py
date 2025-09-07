import threading
import types

import core.executor as executor
from utils.state import StateManager


def test_no_duplicate_orders_with_lock(monkeypatch):
    StateManager.clear()

    start_evt = threading.Event()
    release_evt = threading.Event()
    submit_calls = []

    def fake_submit_order(**kwargs):
        submit_calls.append(kwargs)
        return True, "1"

    status = types.SimpleNamespace(state="filled", filled_qty=1, filled_avg_price=10)

    def fake_wait(coid, timeout_sec):
        start_evt.set()
        release_evt.wait()
        return status

    monkeypatch.setattr(executor.broker, "order_exists", lambda client_order_id: False)
    monkeypatch.setattr(executor.broker, "submit_order", fake_submit_order)
    monkeypatch.setattr(executor, "_wait_for_fill_or_timeout", fake_wait)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    t = threading.Thread(
        target=executor.place_order_with_trailing_stop,
        args=("AAPL", "buy", 1, "market", None, {}),
    )
    t.start()
    start_evt.wait()
    # While first thread holds lock, second call should skip
    res = executor.place_order_with_trailing_stop("AAPL", "buy", 1, "market", None, {})
    assert res is False
    release_evt.set()
    t.join()
    assert len(submit_calls) == 1
