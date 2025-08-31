import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import signals.filters as f


class Cfg(dict):
    pass


def test_quiver_override_and_consensus(monkeypatch):
    cfg = Cfg(
        approvals={
            "quiver_override": True,
            "consensus_required": 2,
            "quiver_strong": {
                "recency_hours": 48,
                "score_threshold": 8.0,
                "require_recent_event": True,
            },
        }
    )
    # Override on
    monkeypatch.setattr(f, "_is_quiver_strong", lambda s, cfg: True)
    f._APPROVAL_CACHE.clear()
    assert f.is_symbol_approved("TEST", 80, cfg) is True
    # Override off → consenso 2/3
    monkeypatch.setattr(f, "_is_quiver_strong", lambda s, cfg: False)
    monkeypatch.setattr(
        f,
        "_provider_votes",
        lambda s, cfg: {"Quiver": True, "FinnhubAlpha": True, "FMP": False},
    )
    f._APPROVAL_CACHE.clear()
    assert f.is_symbol_approved("TEST", 80, cfg) is True
    # 1/3 → falla
    monkeypatch.setattr(
        f,
        "_provider_votes",
        lambda s, cfg: {"Quiver": False, "FinnhubAlpha": True, "FMP": False},
    )
    f._APPROVAL_CACHE.clear()
    assert f.is_symbol_approved("TEST", 80, cfg) is False
