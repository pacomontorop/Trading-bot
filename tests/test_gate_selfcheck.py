from __future__ import annotations

from datetime import datetime, timezone

import config
from core import market_gate
from signals import reader as signals_reader


def _with_policy(policy: dict, fn) -> None:
    original = config._policy
    config._policy = policy
    try:
        fn()
    finally:
        config._policy = original


def test_quiver_gate_disabled_when_thresholds_zero() -> None:
    policy = {
        "quiver_gate": {
            "enabled": True,
            "insider_buy_min_count_lookback": 0,
            "gov_contract_min_total_amount": 0,
            "gov_contract_min_count": 0,
            "patent_momentum_min": 0,
            "sec13f_count_min": 0,
            "sec13f_change_min_pct": 0,
        }
    }

    def _check() -> None:
        ok, reasons = signals_reader.gate_quiver_minimum({})
        assert ok is True
        assert reasons == ["quiver_disabled"]

    _with_policy(policy, _check)


def test_quiver_gate_enabled_rejects_without_signals() -> None:
    policy = {
        "quiver_gate": {
            "enabled": True,
            "insider_buy_min_count_lookback": 1,
            "gov_contract_min_total_amount": 0,
            "gov_contract_min_count": 0,
            "patent_momentum_min": 0,
            "sec13f_count_min": 0,
            "sec13f_change_min_pct": 0,
        }
    }

    def _check() -> None:
        ok, reasons = signals_reader.gate_quiver_minimum({})
        assert ok is False
        assert "quiver_min_signal" in reasons

    _with_policy(policy, _check)


def test_market_gate_fetches_from_alpaca_clock() -> None:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    _next_open = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
    _next_close = datetime(2025, 1, 2, 21, 0, tzinfo=timezone.utc)

    # Note: class bodies cannot reference enclosing function locals by the same
    # name, so we use prefixed names and assign them here.
    class StubClock:
        is_open = True
        next_open = _next_open
        next_close = _next_close

    open_now, source, returned_open, returned_close = market_gate._fetch_alpaca_state(
        now, clock=StubClock()
    )
    assert source == "alpaca"
    assert open_now is True
    assert returned_open == _next_open
    assert returned_close == _next_close
