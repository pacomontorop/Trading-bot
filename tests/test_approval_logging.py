import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import signals.filters as f


class Cfg(dict):
    pass


def test_approval_logging(monkeypatch):
    messages = []
    monkeypatch.setattr(f, "log_event", lambda msg: messages.append(msg))
    cfg = Cfg(approvals={"quiver_override": True, "consensus_required": 2})
    # Override message
    monkeypatch.setattr(f, "_is_quiver_strong", lambda s, cfg: True)
    f._APPROVAL_CACHE.clear()
    assert f.is_symbol_approved("T1", 80, cfg) is True
    assert any("Quiver OVERRIDE" in m for m in messages)
    # Consensus message
    messages.clear()
    monkeypatch.setattr(f, "_is_quiver_strong", lambda s, cfg: False)
    monkeypatch.setattr(
        f,
        "_provider_votes",
        lambda s, cfg: {"Quiver": True, "FinnhubAlpha": True, "FMP": False},
    )
    f._APPROVAL_CACHE.clear()
    assert f.is_symbol_approved("T2", 80, cfg) is True
    assert any("Consenso" in m for m in messages)
