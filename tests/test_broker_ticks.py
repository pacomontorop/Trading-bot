import importlib
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

from core.broker import round_to_tick
from core import executor


def test_round_to_tick_modes():
    assert round_to_tick(70.3266, 0.01, mode="up") == pytest.approx(70.33)
    assert round_to_tick(70.3266, 0.01, mode="down") == pytest.approx(70.32)


def test_apply_tick_rounding_long(monkeypatch):
    # Ensure we use the latest policy configuration
    import config

    importlib.reload(config)
    tick, stop, take_profit, trail = executor._apply_tick_rounding(
        symbol="TEST",
        side="buy",
        entry_price=10.15,
        asset_class="equity",
        stop_price=10.049,
        take_profit=10.512,
        trail_price=0.127,
        cfg=config._policy,
    )
    assert tick == pytest.approx(0.01)
    assert stop == pytest.approx(round_to_tick(10.049, tick, mode="down"))
    assert take_profit == pytest.approx(round_to_tick(10.512, tick, mode="up"))
    assert trail == pytest.approx(round_to_tick(0.127, tick))


def test_partial_take_profit_respects_tick():
    result = executor.compute_partial_take_profit(10.0, 0.5, cfg={}, tick=0.01)
    expected = round_to_tick(10.0 + 1.5 * 0.5, 0.01, mode="up")
    assert result == pytest.approx(expected)
