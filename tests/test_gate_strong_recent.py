import os, sys, importlib
from datetime import date
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import signals.gates as gates
from signals import quiver_utils
from signals.quiver_utils import SignalResult
from utils import state


def setup_env(monkeypatch, insider_days=None, gov_days=None, patent_days=None):
    importlib.reload(state)
    try:
        os.remove(os.path.join("data", f"state_{date.today():%Y%m%d}.json"))
    except FileNotFoundError:
        pass
    monkeypatch.setattr(gates, "already_evaluated_today", lambda s: False)
    monkeypatch.setattr(gates, "already_executed_today", lambda s: False)
    monkeypatch.setattr(gates, "is_market_open", lambda: True)
    monkeypatch.setattr(gates, "is_blacklisted_recent_loser", lambda s: False)
    monkeypatch.setattr(gates, "fetch_yfinance_stock_data", lambda s: (600_000_000, 600_000, None, None, None, None))
    monkeypatch.setattr(gates, "get_current_price", lambda s: 10.0)
    monkeypatch.setattr(quiver_utils, "get_insider_signal", lambda s: SignalResult(True, insider_days))
    monkeypatch.setattr(quiver_utils, "get_gov_contract_signal", lambda s: SignalResult(True, gov_days))
    monkeypatch.setattr(quiver_utils, "get_patent_momentum_signal", lambda s: SignalResult(True, patent_days))


def test_gate_passes_if_any_recent(monkeypatch):
    setup_env(monkeypatch, insider_days=1, gov_days=5, patent_days=10)
    ok, _ = gates.passes_long_gate("AAPL")
    assert ok


def test_gate_fails_if_all_stale(monkeypatch):
    setup_env(monkeypatch, insider_days=5, gov_days=6, patent_days=7)
    ok, reasons = gates.passes_long_gate("AAPL")
    assert not ok
    assert "quiver" in reasons
