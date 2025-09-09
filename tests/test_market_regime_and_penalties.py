import utils.market_regime as mr
from signals import scoring


class Cfg(dict):
    pass


def test_exposure_from_regime_bounds():
    cfg = Cfg(market={"min_exposure": 0.6, "max_exposure": 1.0})
    assert 0.6 <= mr.exposure_from_regime(cfg, "high_vol") <= 1.0
    assert 0.6 <= mr.exposure_from_regime(cfg, "elevated_vol") <= 1.0
    assert mr.exposure_from_regime(cfg, "normal") == 1.0


def test_atr_z_penalty_kicks_in(monkeypatch):
    cfg = Cfg(score={"atr_z_penalty": {"lookback_days": 5, "z_threshold": 0.5, "max_penalty": 10}})
    atrs = [1, 1, 1, 1, 3]
    pen = scoring._atr_z_penalty(atrs, cfg)
    assert pen < 0


def test_gap_rejection_penalty():
    cfg = Cfg(score={"gap_open_rejection": {"lookback_minutes": 15, "weakness_threshold_pct": -0.3, "penalty": 5}})
    prev_close = 100.0
    open_price = 103.0
    first15 = [102.9, 102.5, 101.9]
    pen = scoring._gap_open_rejection_penalty(open_price, first15, prev_close, cfg)
    assert pen == -5
    pen2 = scoring._gap_open_rejection_penalty(99.0, first15, prev_close, cfg)
    assert pen2 == 0.0


def test_compute_vix_regime_uses_cache(monkeypatch):
    monkeypatch.setattr(mr, "_CACHE", {})
    monkeypatch.setattr(
        mr,
        "_get_recent_vix_levels",
        lambda wins: [20, 19, 18, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30],
    )
    cfg = {
        "market": {
            "vix_percentile_windows": [1, 5, 10],
            "vix_high_pct": 80,
            "vix_elevated_pct": 60,
            "cache_ttl_sec": 3600,
        }
    }
    a = mr.compute_vix_regime(cfg)
    b = mr.compute_vix_regime(cfg)
    assert a["regime"] in ("normal", "elevated_vol", "high_vol")
    assert b == a
