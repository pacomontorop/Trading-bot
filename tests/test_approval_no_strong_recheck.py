import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import signals.filters as filters
import signals.gates as gates


def test_approval_does_not_call_gate(monkeypatch):
    monkeypatch.setattr(
        gates,
        "_has_strong_recent_quiver_signal",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("should not be called")),
    )
    monkeypatch.setattr(filters, "_is_quiver_strong", lambda s, cfg: False)
    monkeypatch.setattr(
        filters,
        "_provider_votes",
        lambda s, cfg: {"Quiver": True, "FinnhubAlpha": True, "FMP": False},
    )
    filters._APPROVAL_CACHE.clear()
    cfg = {"approvals": {"quiver_override": False, "consensus_required": 2}}
    assert filters.is_symbol_approved("AAPL", 0, cfg) is True
