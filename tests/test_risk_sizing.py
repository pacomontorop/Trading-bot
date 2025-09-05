import math
import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_sizing_scales_with_atr_and_equity():
    from core.executor import calculate_position_size_risk_based
    class Cfg(dict):
        pass
    cfg = Cfg(risk={"max_symbol_risk_pct":0.5,"atr_k":2.0,"min_stop_pct":0.05,"allow_fractional":True})
    r = calculate_position_size_risk_based("AAA", price=100, atr=1, equity=100000, cfg=cfg, market_exposure_factor=1.0)
    assert abs(r["shares"] - 100) < 1e-6
    assert abs(r["notional"] - 10000) < 1e-6

def test_caps_and_fractional_behavior():
    from core.executor import calculate_position_size_risk_based
    class Cfg(dict):
        pass
    cfg = Cfg(risk={"max_symbol_risk_pct":0.35,"atr_k":2.0,"min_stop_pct":0.05,"allow_fractional":False})
    equity = 20000
    r = calculate_position_size_risk_based("BBB", price=50, atr=0.3, equity=equity, cfg=cfg, market_exposure_factor=1.0)
    assert r["notional"] <= 0.10 * equity
    cfg2 = Cfg(risk={"max_symbol_risk_pct":0.01,"atr_k":2.0,"min_stop_pct":0.05,"allow_fractional":False})
    r2 = calculate_position_size_risk_based("CCC", price=500, atr=0.1, equity=equity, cfg=cfg2, market_exposure_factor=1.0)
    assert r2["shares"] == 0 or r2["notional"] == 0

def test_exposure_factor_affects_notional():
    from core.executor import calculate_position_size_risk_based
    class Cfg(dict):
        pass
    cfg = Cfg(risk={"max_symbol_risk_pct":0.35,"atr_k":2.0,"min_stop_pct":0.05,"allow_fractional":True})
    base = calculate_position_size_risk_based("DDD", price=100, atr=1, equity=50000, cfg=cfg, market_exposure_factor=1.0)
    reduced = calculate_position_size_risk_based("DDD", price=100, atr=1, equity=50000, cfg=cfg, market_exposure_factor=0.7)
    assert reduced["notional"] < base["notional"]
    assert abs(reduced["notional"]/base["notional"] - 0.7) < 1e-6

def test_min_stop_pct_when_atr_small():
    from core.executor import calculate_position_size_risk_based
    class Cfg(dict):
        pass
    cfg = Cfg(risk={"max_symbol_risk_pct":0.35,"atr_k":2.0,"min_stop_pct":0.05,"allow_fractional":True})
    r = calculate_position_size_risk_based("EEE", price=100, atr=0.1, equity=100000, cfg=cfg)
    assert abs(r["stop_distance"] - 5.0) < 1e-6
