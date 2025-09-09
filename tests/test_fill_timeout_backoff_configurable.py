import types
import core.executor as executor
from utils.state import StateManager


def test_fill_timeout_and_backoff_configurable(monkeypatch):
    StateManager.clear()

    captured = {}

    def fake_order_exists(client_order_id):
        return False

    def fake_submit_order(**kwargs):
        return True, "1"

    def fake_wait(coid, timeout_sec, initial_delay, backoff_factor, max_delay):
        captured["params"] = (timeout_sec, initial_delay, backoff_factor, max_delay)
        return types.SimpleNamespace(state="timeout")

    monkeypatch.setattr(executor.broker, "order_exists", fake_order_exists)
    monkeypatch.setattr(executor.broker, "submit_order", fake_submit_order)
    monkeypatch.setattr(executor, "_wait_for_fill_or_timeout", fake_wait)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    cfg = {
        "broker": {
            "fill_timeout_sec": 7,
            "fill_initial_delay_sec": 1.0,
            "fill_backoff_factor": 2.5,
            "fill_max_delay_sec": 3.0,
        }
    }
    executor.place_order_with_trailing_stop("AAPL", "buy", 1, "market", None, cfg)
    assert captured["params"] == (7, 1.0, 2.5, 3.0)
