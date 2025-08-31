import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
os.environ.setdefault("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from core.executor import calculate_investment_amount


class Cfg:
    pass


def test_calculate_investment_amount_scaling(monkeypatch):
    equity = 50000
    cfg = Cfg()
    monkeypatch.setattr("core.executor.get_market_exposure_factor", lambda cfg: 1.0)
    assert calculate_investment_amount(0, equity, cfg) == 2000
    assert calculate_investment_amount(100, equity, cfg) == min(3000, 0.10 * equity)
    mid = calculate_investment_amount(50, equity, cfg)
    assert 2000 < mid <= 3000
