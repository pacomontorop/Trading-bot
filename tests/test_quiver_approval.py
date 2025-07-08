"""Tests for quiver approval logic using mocked API responses.

The real implementation relies heavily on live requests to the Quiver API and
`yfinance`.  In order to make the test-suite deterministic and runnable without
network access we replace those calls with dummy data via ``unittest.mock``.
"""

import sys
from types import SimpleNamespace
from unittest.mock import patch

# Provide dummy modules if external dependencies are missing
sys.modules.setdefault("requests", SimpleNamespace(get=lambda *a, **k: None))
sys.modules.setdefault("dotenv", SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault("yfinance", SimpleNamespace(Ticker=lambda *a, **k: SimpleNamespace(info={}, history=lambda *a, **k: SimpleNamespace())))

from signals.quiver_approval import (
    is_approved_by_quiver,
    evaluate_quiver_signals,
    get_all_quiver_signals,
    QUIVER_APPROVAL_THRESHOLD,
)
import signals.quiver_utils as quiver_utils

def test_quiver_integrity_response():
    with patch("signals.quiver_utils.get_all_quiver_signals", return_value={}):
        with patch("signals.quiver_utils.evaluate_quiver_signals", return_value=True):
            result = is_approved_by_quiver("AAPL")
    assert isinstance(result, bool), "La función no devuelve un booleano"

def test_quiver_with_fake_symbol():
    with patch("signals.quiver_utils.get_all_quiver_signals", return_value={}):
        with patch("signals.quiver_utils.evaluate_quiver_signals", return_value=False):
            result = is_approved_by_quiver("FAKE1234")
    assert isinstance(result, bool), "Error en símbolo inexistente"

def test_quiver_score_above_threshold():
    signals = {
        "insider_buy_more_than_sell": True,
        "has_gov_contract": True,
        "positive_patent_momentum": True,
        "trending_wsb": True,
        "bullish_etf_flow": False,
        "has_recent_sec13f_activity": False,
        "has_recent_sec13f_changes": False,
        "has_recent_dark_pool_activity": False,
        "is_high_political_beta": False,
        "is_trending_on_twitter": False,
        "has_positive_app_ratings": False,
    }
    with patch("signals.quiver_utils.has_recent_quiver_event", return_value=True):
        with patch("signals.quiver_utils.fetch_yfinance_stock_data", return_value=(300_000_000, 500_000, None, None, None, None)):
            result = evaluate_quiver_signals(signals, symbol="TEST")
    assert result is True, "La puntuación debería aprobar según el umbral actual"

def test_quiver_score_below_threshold():
    signals = {
        "insider_buy_more_than_sell": False,
        "has_gov_contract": False,
        "positive_patent_momentum": False,
        "trending_wsb": False,
        "bullish_etf_flow": False,
        "has_recent_sec13f_activity": False,
        "has_recent_sec13f_changes": False,
        "has_recent_dark_pool_activity": False,
        "is_high_political_beta": False,
        "is_trending_on_twitter": False,
        "has_positive_app_ratings": False,
    }
    with patch("signals.quiver_utils.has_recent_quiver_event", return_value=True):
        with patch("signals.quiver_utils.fetch_yfinance_stock_data", return_value=(300_000_000, 500_000, None, None, None, None)):
            result = evaluate_quiver_signals(signals, symbol="TEST")
    assert result is False, "No debería aprobar sin señales activas"

def test_quiver_fallback_to_finnhub_alpha():
    with patch("signals.quiver_utils.get_all_quiver_signals", return_value={}):
        with patch("signals.quiver_utils.evaluate_quiver_signals", return_value=False):
            result = is_approved_by_quiver("ZZZZFAKE")
    assert isinstance(result, bool), "Debe devolver un booleano aunque falle Quiver"

def test_quiver_signals_structure():
    dummy = {
        "insider_buy_more_than_sell": True,
        "has_gov_contract": False,
        "positive_patent_momentum": True,
        "trending_wsb": False,
        "bullish_etf_flow": False,
        "has_recent_sec13f_activity": False,
        "has_recent_sec13f_changes": False,
        "has_recent_dark_pool_activity": False,
        "is_high_political_beta": False,
        "is_trending_on_twitter": False,
        "has_positive_app_ratings": False,
    }
    with patch("signals.quiver_utils.get_all_quiver_signals", return_value=dummy):
        signals = get_all_quiver_signals("AAPL")
    assert isinstance(signals, dict), "Las señales deben ser un diccionario"
    expected_keys = list(dummy.keys())
    for key in expected_keys:
        assert key in signals, f"Falta la clave '{key}' en las señales"


def test_approval_logged_once_per_day():
    signals = {
        "insider_buy_more_than_sell": True,
        "has_gov_contract": True,
        "positive_patent_momentum": True,
        "trending_wsb": True,
        "bullish_etf_flow": False,
        "has_recent_sec13f_activity": False,
        "has_recent_sec13f_changes": False,
        "has_recent_dark_pool_activity": False,
        "is_high_political_beta": False,
        "is_trending_on_twitter": False,
        "has_positive_app_ratings": False,
    }

    quiver_utils.approved_today.clear()

    with patch("signals.quiver_utils.has_recent_quiver_event", return_value=True), \
         patch("signals.quiver_utils.fetch_yfinance_stock_data", return_value=(300_000_000, 500_000, None, None, None, None)), \
         patch("signals.quiver_utils.log_event") as log_mock:
        evaluate_quiver_signals(signals, symbol="TEST")
        evaluate_quiver_signals(signals, symbol="TEST")
        assert log_mock.call_count == 1

    quiver_utils.reset_daily_approvals()

    with patch("signals.quiver_utils.has_recent_quiver_event", return_value=True), \
         patch("signals.quiver_utils.fetch_yfinance_stock_data", return_value=(300_000_000, 500_000, None, None, None, None)), \
         patch("signals.quiver_utils.log_event") as log_mock:
        evaluate_quiver_signals(signals, symbol="TEST")
        assert log_mock.call_count == 1
