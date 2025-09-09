import core.executor as executor
from utils.state import StateManager


def test_lock_released_and_reconcile_called(monkeypatch):
    StateManager.clear()

    def fake_order_exists(client_order_id):
        return False

    def fake_submit_order(**kwargs):
        raise RuntimeError("boom")

    reconciled = {}

    def fake_reconcile(symbol, coid, cfg):
        reconciled["symbol"] = symbol
        reconciled["coid"] = coid

    monkeypatch.setattr(executor.broker, "order_exists", fake_order_exists)
    monkeypatch.setattr(executor.broker, "submit_order", fake_submit_order)
    monkeypatch.setattr(executor, "_safe_reconcile_by_coid", fake_reconcile)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    res = executor.place_order_with_trailing_stop("AAPL", "buy", 1, "market", None, {})
    assert res is False
    assert reconciled["symbol"] == "AAPL"
    lock = executor._get_symbol_lock("AAPL")
    assert lock.acquire(blocking=False)
    lock.release()
