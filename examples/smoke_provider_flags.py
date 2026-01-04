#!/usr/bin/env python3
"""Smoke test provider combinations without external requests."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _set_flags(*, quiver: bool, yahoo: bool, fmp: bool) -> None:
    os.environ["ENABLE_QUIVER"] = "true" if quiver else "false"
    os.environ["ENABLE_YAHOO"] = "true" if yahoo else "false"
    os.environ["ENABLE_FMP"] = "true" if fmp else "false"


def _stub_providers(config_module) -> None:
    if config_module.ENABLE_QUIVER:
        from signals import quiver_utils

        quiver_utils.fetch_quiver_signals = lambda symbol: {}

    if config_module.ENABLE_YAHOO:
        from signals import scoring

        def _fake_fetch(symbol, verbose: bool = False, return_history: bool = False):
            data = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            if return_history:
                return data, None
            return data

        scoring.fetch_yfinance_stock_data = _fake_fetch

    if config_module.ENABLE_FMP:
        from signals import fmp_utils

        fmp_utils.grades_news = lambda symbol, page=0, limit=1: []
        fmp_utils.ratings_snapshot = lambda symbol, limit=1: []


def _run_case(name: str, *, quiver: bool, yahoo: bool, fmp: bool) -> None:
    _set_flags(quiver=quiver, yahoo=yahoo, fmp=fmp)
    import config as config_module

    importlib.reload(config_module)
    import signals.features as features
    import signals.reader as reader

    importlib.reload(features)
    importlib.reload(reader)
    _stub_providers(config_module)

    feature_map = features.get_symbol_features("AAPL")
    score = reader._score_from_features(feature_map)

    assert isinstance(score, float), f"{name}: score not float"
    if not config_module.ENABLE_FMP:
        assert not any(k.startswith("fmp_") for k in feature_map), f"{name}: fmp key found"
        assert not any(k.startswith("fmp_") for k in reader.FEATURE_WEIGHTS), f"{name}: fmp weight found"
    if not config_module.ENABLE_QUIVER:
        assert not any(k.startswith("quiver_") for k in reader.FEATURE_WEIGHTS), f"{name}: quiver weight found"
    if not config_module.ENABLE_YAHOO:
        assert not any(k.startswith("yahoo_") for k in reader.FEATURE_WEIGHTS), f"{name}: yahoo weight found"


def main() -> None:
    _run_case("A", quiver=True, yahoo=True, fmp=False)
    _run_case("B", quiver=True, yahoo=False, fmp=False)
    _run_case("C", quiver=False, yahoo=True, fmp=False)
    _run_case("D", quiver=True, yahoo=True, fmp=True)
    print("Smoke tests passed.")


if __name__ == "__main__":
    main()
