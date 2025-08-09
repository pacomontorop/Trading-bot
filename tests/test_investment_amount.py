import os

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")
os.environ.setdefault("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

from core import executor

class DummyAccount:
    def __init__(self, equity):
        self.equity = str(equity)


def test_calculate_investment_amount_limits(monkeypatch):
    # Patch API to return fixed equity
    monkeypatch.setattr(executor.api, "get_account", lambda: DummyAccount(10000))

    executor.reset_daily_investment()
    amount = executor.calculate_investment_amount(19)
    # Máximo 10% del equity
    assert amount == 1000

    # Simular capital ya invertido para acercarnos al límite diario (50% equity)
    executor.add_to_invested(4600)
    amount2 = executor.calculate_investment_amount(19)
    # Solo queda disponible 400 USD del límite diario
    assert amount2 == 400

