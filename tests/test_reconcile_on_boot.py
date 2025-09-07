import types
import importlib

import broker.alpaca as alpaca
from utils.state import StateManager


def test_reconcile_on_boot(monkeypatch):
    StateManager.clear()

    dummy_account = types.SimpleNamespace(buying_power=0, equity=0)

    class _T:
        def astimezone(self, tz):
            return 0

    dummy_clock = types.SimpleNamespace(next_open=_T())
    monkeypatch.setattr(alpaca.api, "get_account", lambda: dummy_account)
    monkeypatch.setattr(alpaca.api, "get_clock", lambda: dummy_clock)

    scheduler = importlib.reload(__import__("core.scheduler", fromlist=["scheduler"]))

    order = types.SimpleNamespace(symbol="AAPL", client_order_id="123")
    position = types.SimpleNamespace(symbol="MSFT", qty=2, avg_entry_price=10.0)

    monkeypatch.setattr(scheduler.broker, "list_open_orders_today", lambda: [order])
    monkeypatch.setattr(scheduler.broker, "list_positions", lambda: [position])
    monkeypatch.setattr(scheduler, "log_event", lambda *a, **k: None)

    scheduler.reconcile_on_boot()

    assert StateManager.get_open_orders() == {"AAPL": "123"}
    assert "MSFT" in StateManager.get_open_positions()
