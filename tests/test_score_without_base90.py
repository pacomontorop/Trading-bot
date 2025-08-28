import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import signals.scoring as scoring


def test_score_range_and_components():
    market_data = {
        "quiver": {"insiders": 1, "gov_contract": 2},
        "fmp": {"ratings_snapshot": 8, "rsi": 40, "news_polarity": 1},
        "atr_ratio": 0,
        "gap": 0,
        "macro_vix": 0,
    }
    res = scoring.score_long_signal("AAPL", market_data)
    assert 0 <= res["score"] <= 100
    assert "quiver" in res["components"]
    assert res["score"] != 90
