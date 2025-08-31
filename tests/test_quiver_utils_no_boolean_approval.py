import os, sys, asyncio
os.environ.setdefault("APCA_API_KEY_ID", "key")
os.environ.setdefault("APCA_API_SECRET_KEY", "secret")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from signals import quiver_utils as q


def test_quiver_evaluation_returns_dict(monkeypatch):
    dummy = {"score": 0.0, "active_signals": []}
    async def fake_async(symbol):
        return dummy
    monkeypatch.setattr(q, "_async_is_approved_by_quiver", fake_async)
    monkeypatch.setattr(q, "run_in_quiver_loop", lambda coro: asyncio.get_event_loop().run_until_complete(coro))
    res = q.is_approved_by_quiver("AAPL")
    assert isinstance(res, dict)
    res2 = q.evaluate_quiver_signals({}, "AAPL")
    assert isinstance(res2, dict)
