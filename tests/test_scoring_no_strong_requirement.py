import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import signals.scoring as scoring


def test_scoring_accepts_weak_signals_only():
    market_data = {
        "quiver": {"wsb": 2, "twitter": 1},
        "fmp": {},
        "atr_ratio": 0,
        "gap": 0,
        "macro_vix": 0,
    }
    res = scoring.score_long_signal("AAPL", market_data)
    assert 0 <= res["score"] <= 100
