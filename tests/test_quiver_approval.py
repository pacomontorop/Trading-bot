from signals.quiver_approval import (
    is_approved_by_quiver,
    evaluate_quiver_signals,
    get_all_quiver_signals,
    QUIVER_APPROVAL_THRESHOLD
)

def test_quiver_integrity_response():
    result = is_approved_by_quiver("AAPL")
    assert isinstance(result, bool), "La función no devuelve un booleano"

def test_quiver_with_fake_symbol():
    result = is_approved_by_quiver("FAKE1234")
    assert isinstance(result, bool), "Error en símbolo inexistente"

def test_quiver_score_above_threshold():
    signals = {
        "insider_buy_more_than_sell": True,
        "has_gov_contract": True,
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
    result = evaluate_quiver_signals(signals, symbol="TEST")
    assert result is False, "No debería aprobar sin señales activas"

def test_quiver_fallback_to_finnhub_alpha():
    result = is_approved_by_quiver("ZZZZFAKE")
    assert isinstance(result, bool), "Debe devolver un booleano aunque falle Quiver"

def test_quiver_signals_structure():
    signals = get_all_quiver_signals("AAPL")
    assert isinstance(signals, dict), "Las señales deben ser un diccionario"
    expected_keys = [
        "insider_buy_more_than_sell", "has_gov_contract", "positive_patent_momentum",
        "trending_wsb", "bullish_etf_flow", "has_recent_sec13f_activity", "has_recent_sec13f_changes",
        "has_recent_dark_pool_activity", "is_high_political_beta", "is_trending_on_twitter", "has_positive_app_ratings"
    ]
    for key in expected_keys:
        assert key in signals, f"Falta la clave '{key}' en las señales"
