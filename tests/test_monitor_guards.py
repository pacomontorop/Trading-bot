import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

from core import monitor


def test_monitor_guard_skips_invalid_inputs(monkeypatch):
    events = []
    monkeypatch.setattr(monitor, "log_event", lambda message, **_: events.append(message))
    monkeypatch.setattr(monitor, "get_current_price", lambda symbol: 1.0)

    monitor.check_virtual_take_profit_and_stop(
        symbol="XYZ",
        entry_price=None,
        qty=0,
        qty_available=0,
        position_side="long",
        asset_class="us_equity",
    )

    assert any("skip" in msg for msg in events)
