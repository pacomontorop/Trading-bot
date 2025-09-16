import os, sys
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_symbol_cache_prevents_recompute(monkeypatch):
    from signals.quiver_utils import fetch_quiver_signals
    calls = {"n": 0}

    def fake(symbol):
        calls["n"] += 1
        return {"ok": True}

    monkeypatch.setattr("signals.quiver_utils.get_all_quiver_signals", fake)
    a = fetch_quiver_signals("TEST")
    b = fetch_quiver_signals("TEST")
    assert calls["n"] == 1


def test_heavy_endpoint_cached(monkeypatch):
    from signals.quiver_utils import _cached_heavy_endpoint
    calls = {"n": 0}

    def fake(url):
        calls["n"] += 1
        return [{"Ticker": "AAA"}]

    monkeypatch.setattr("signals.quiver_utils.safe_quiver_request", fake)
    data1 = _cached_heavy_endpoint("demo", "http://example", 99999)
    data2 = _cached_heavy_endpoint("demo", "http://example", 99999)
    assert calls["n"] == 1 and isinstance(data2, list)


def test_skip_externals_when_score_low(monkeypatch):
    from signals.reader import maybe_fetch_externals

    class Cfg(dict):
        pass

    cfg = Cfg(cache={"score_recalc_threshold": 60})
    res = maybe_fetch_externals("LOW", 55, cfg)
    assert res is None
