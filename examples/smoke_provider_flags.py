#!/usr/bin/env python3
"""Smoke test provider combinations without external requests."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _set_flags(*, quiver: bool, yahoo: bool) -> None:
    os.environ["ENABLE_QUIVER"] = "true" if quiver else "false"
    os.environ["ENABLE_YAHOO"] = "true" if yahoo else "false"


def _stub_providers(config_module) -> None:
    if config_module.ENABLE_QUIVER:
        from signals import quiver_utils

        quiver_utils.fetch_quiver_signals = lambda symbol, **kwargs: {}

    if config_module.ENABLE_YAHOO:
        from signals import scoring

        def _fake_fetch(symbol, *args, **kwargs):
            data = (0.0, 0.0, 0.0, True, 0.0, 0.0, 10.0, 1.0)
            snapshot = scoring.YahooSnapshot(data, symbol, False, "ok")
            if kwargs.get("return_history"):
                return snapshot, None
            return snapshot

        scoring.fetch_yahoo_snapshot = _fake_fetch


def _run_case(name: str, *, quiver: bool, yahoo: bool) -> None:
    _set_flags(quiver=quiver, yahoo=yahoo)
    import config as config_module

    importlib.reload(config_module)
    import signals.features as features
    import signals.reader as reader

    importlib.reload(features)
    importlib.reload(reader)
    _stub_providers(config_module)

    feature_map = features.get_symbol_features("AAPL")
    score, _ = reader._score_from_features(feature_map)

    assert isinstance(score, float), f"{name}: score not float"
    if not config_module.ENABLE_QUIVER:
        assert not any(k.startswith("quiver_") for k in feature_map), f"{name}: quiver key found"
    if not config_module.ENABLE_YAHOO:
        assert not any(k.startswith("yahoo_") for k in feature_map), f"{name}: yahoo key found"


def _test_disable_quiver_import() -> None:
    _set_flags(quiver=False, yahoo=True)
    import config as config_module

    importlib.reload(config_module)
    import signals.features as features

    importlib.reload(features)
    sys.modules.pop("signals.quiver_utils", None)
    _stub_providers(config_module)
    features.get_symbol_features("AAPL")
    assert "signals.quiver_utils" not in sys.modules, "quiver_utils imported with ENABLE_QUIVER=false"


def _test_yahoo_disabled_no_trade() -> None:
    _set_flags(quiver=True, yahoo=False)
    import config as config_module

    importlib.reload(config_module)
    import signals.reader as reader

    importlib.reload(reader)
    _stub_providers(config_module)
    reader._load_universe = lambda path="data/symbols.csv": [
        {"symbol": "AAPL", "ticker_map": {"canonical": "AAPL", "yahoo": "AAPL", "quiver": "AAPL"}}
    ]
    approvals = reader.get_top_signals(max_symbols=1)
    assert not approvals, "expected no trades when Yahoo is disabled"


def _test_risk_limits_block() -> None:
    from core import risk_manager

    state = risk_manager.DailyRiskState(date="2099-01-01", spent_today_usd=1000, new_positions_today=3)
    snapshot = {
        "equity": 10000.0,
        "cash": 500.0,
        "positions": [],
        "orders": [],
        "total_exposure": 0.0,
        "symbol_exposure": {},
    }
    ok, reasons = risk_manager.check_risk_limits(
        symbol="AAPL",
        state=state,
        snapshot=snapshot,
        planned_spend=50.0,
    )
    assert not ok and reasons, "risk limits should block when daily limits exceeded"


def main() -> None:
    _run_case("A", quiver=True, yahoo=True)
    _run_case("B", quiver=True, yahoo=False)
    _run_case("C", quiver=False, yahoo=True)
    _test_disable_quiver_import()
    _test_yahoo_disabled_no_trade()
    _test_risk_limits_block()
    print("Smoke tests passed.")


if __name__ == "__main__":
    main()
