
def test_exposure_factor_modulates_size(monkeypatch):
    from core.executor import calculate_investment_amount

    class Cfg:
        pass

    cfg = Cfg()
    equity = 20000

    monkeypatch.setattr("core.executor.get_market_exposure_factor", lambda cfg: 1.0)
    full = calculate_investment_amount(80, equity, cfg)

    monkeypatch.setattr("core.executor.get_market_exposure_factor", lambda cfg: 0.7)
    reduced = calculate_investment_amount(80, equity, cfg)

    assert reduced < full and abs(reduced / full - 0.7) < 1e-6
