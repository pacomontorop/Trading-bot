from datetime import datetime, timedelta, timezone


def test_minutes_to_close_cutoff(monkeypatch):
    from core.executor import _apply_event_and_cutoff_policies
    from utils import market_calendar

    now = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        market_calendar, "next_session_close_utc", lambda: now + timedelta(minutes=5)
    )

    class Cfg(dict):
        pass

    cfg = Cfg(market={"avoid_last_minutes": 10})
    ok, notional, reason = _apply_event_and_cutoff_policies("AAA", 1000.0, cfg)
    assert ok is False and "cutoff_last_10m" in reason


def test_event_block_and_reduce(monkeypatch):
    from core.executor import _apply_event_and_cutoff_policies

    class Cfg(dict):
        pass

    cfg_block = Cfg(
        market={"avoid_earnings_days": 3, "event_block_mode": "block", "avoid_last_minutes": 0}
    )
    cfg_reduce = Cfg(
        market={
            "avoid_earnings_days": 3,
            "event_block_mode": "reduce",
            "event_reduce_fraction": 0.5,
            "avoid_last_minutes": 0,
        }
    )

    monkeypatch.setattr(
        "utils.market_calendar.earnings_within", lambda symbol, days: True
    )
    ok, notional, reason = _apply_event_and_cutoff_policies("TEST", 1000.0, cfg_block)
    assert ok is False and "event_block" in reason

    ok, notional, reason = _apply_event_and_cutoff_policies("TEST", 1000.0, cfg_reduce)
    assert ok is True and abs(notional - 500.0) < 1e-6


def test_no_event_no_cutoff(monkeypatch):
    from core.executor import _apply_event_and_cutoff_policies

    class Cfg(dict):
        pass

    cfg = Cfg(market={"avoid_earnings_days": 3, "avoid_last_minutes": 0})
    monkeypatch.setattr("utils.market_calendar.earnings_within", lambda s, d: False)
    ok, notional, reason = _apply_event_and_cutoff_policies("TEST", 1000.0, cfg)
    assert ok is True and abs(notional - 1000.0) < 1e-6 and reason == "ok"
