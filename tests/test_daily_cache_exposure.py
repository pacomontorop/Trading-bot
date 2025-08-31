from datetime import datetime as real_datetime


def test_daily_cache_exposure(monkeypatch):
    from core.executor import get_market_exposure_factor, _market_exposure_state

    class DT1:
        @classmethod
        def utcnow(cls):
            return real_datetime(2023, 1, 1, 12, 0, 0)

    class DT2:
        @classmethod
        def utcnow(cls):
            return real_datetime(2023, 1, 2, 12, 0, 0)

    call_count = {"n": 0}

    def regime_high():
        call_count["n"] += 1
        return "high_vol"

    def regime_normal():
        call_count["n"] += 1
        return "normal"

    cfg = type("Cfg", (), {"market": type("M", (), {})()})()

    monkeypatch.setattr("core.executor.datetime", DT1)
    monkeypatch.setattr("utils.cache.get_cached_market_regime", regime_high)
    _market_exposure_state["last_calculated"] = None
    _market_exposure_state["factor"] = 1.0
    first = get_market_exposure_factor(cfg)
    second = get_market_exposure_factor(cfg)
    assert first == second == 0.7
    assert call_count["n"] == 1

    monkeypatch.setattr("core.executor.datetime", DT2)
    monkeypatch.setattr("utils.cache.get_cached_market_regime", regime_normal)
    third = get_market_exposure_factor(cfg)
    assert third == 1.0
    assert call_count["n"] == 2
