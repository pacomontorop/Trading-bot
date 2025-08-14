import os
import types
from datetime import datetime, timedelta

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

from utils.crypto_limit import CryptoLimit


class DummyApi:
    def __init__(self):
        self._account_calls = 0

    def get_account(self):
        self._account_calls += 1
        return types.SimpleNamespace(buying_power="1000")

    def get_clock(self):
        # next open one hour ahead
        return types.SimpleNamespace(next_open=datetime.utcnow() + timedelta(hours=1))


def test_can_spend_and_limit(monkeypatch):
    dummy = DummyApi()
    monkeypatch.setattr("utils.crypto_limit.get_api", lambda: dummy)
    limit = CryptoLimit()
    assert limit.can_spend(50)
    assert limit.remaining() == limit.max_notional - 50
    # Exceeding should fail
    assert not limit.can_spend(1000)
