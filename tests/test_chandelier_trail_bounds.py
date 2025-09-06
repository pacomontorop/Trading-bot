def test_chandelier_within_min_max():
    from core.executor import compute_chandelier_trail

    class Cfg(dict):
        pass

    cfg = Cfg(risk={"atr_k": 2.0, "min_trailing_pct": 0.005, "max_trailing_pct": 0.05})
    # price=100, atr=0.1 -> atr_k*atr=0.2, min%*price=0.5 -> trail=0.5, ≤ max=5.0
    assert abs(compute_chandelier_trail(100, 0.1, cfg) - 0.5) < 1e-6
    # price=100, atr=5 -> atr_k*atr=10, min=0.5 -> trail=10 capped to 5
    assert abs(compute_chandelier_trail(100, 5, cfg) - 5.0) < 1e-6

