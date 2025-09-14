def test_gate_strong_recent_boundary(monkeypatch):
    from signals import gates
    class R:
        def __init__(self, active, days):
            self.active = active
            self.days = days
    monkeypatch.setattr("signals.quiver_utils.get_insider_signal", lambda s: R(True, 3.0))
    monkeypatch.setattr("signals.quiver_utils.get_gov_contract_signal", lambda s: R(False, None))
    monkeypatch.setattr("signals.quiver_utils.get_patent_momentum_signal", lambda s: R(False, None))
    assert gates._has_strong_recent_quiver_signal("TEST", 3.0) is True
    monkeypatch.setattr("signals.quiver_utils.get_insider_signal", lambda s: R(True, 3.01))
    assert gates._has_strong_recent_quiver_signal("TEST", 3.0) is False

def test_scoring_recency_boost_hours_boundary():
    from signals.scoring import _recency_boost
    assert abs(_recency_boost(2.0, strong_recency_hours=48, k=2.0) - 2.0) < 1e-6
    assert abs(_recency_boost(47.9/24.0, 48, 2.0) - 2.0) < 1e-6
    assert _recency_boost(48.1/24.0, 48, 2.0) < 2.0

def test_days_since_never_negative():
    import signals.quiver_utils as q
    from datetime import datetime, timezone, timedelta
    past = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(hours=1)
    assert q._days_since(past) >= 0.0
