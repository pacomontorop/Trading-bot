import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import signals.filters as f


class Cfg(dict):
    pass


def test_approval_cache(monkeypatch):
    calls = {"count": 0}

    def fake_votes(symbol, cfg):
        calls["count"] += 1
        return {"Quiver": True, "FinnhubAlpha": True, "FMP": False}

    cfg = Cfg(approvals={"quiver_override": False, "consensus_required": 2})
    monkeypatch.setattr(f, "_is_quiver_strong", lambda s, cfg: False)
    monkeypatch.setattr(f, "_provider_votes", fake_votes)
    f._APPROVAL_CACHE.clear()
    assert f.is_symbol_approved("AAA", 80, cfg) is True
    assert calls["count"] == 1
    assert f.is_symbol_approved("AAA", 80, cfg) is True
    assert calls["count"] == 1
