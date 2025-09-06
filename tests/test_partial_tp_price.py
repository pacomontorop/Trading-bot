def test_partial_tp_at_1p5R():
    from core.executor import compute_partial_take_profit

    class Cfg(dict):
        pass

    cfg = Cfg(exits={"use_partial_take_profit": True, "partial_tp_at_R": 1.5})
    entry, stop_dist = 100.0, 4.0
    tp = compute_partial_take_profit(entry, stop_dist, cfg)
    assert abs(tp - 106.0) < 1e-6

