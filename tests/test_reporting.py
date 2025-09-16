def test_metrics_counter_increments():
    import utils.metrics as m

    before = m.get_all().get("approved", 0)
    m.inc("approved", 2)
    vals = m.get_all()
    assert vals.get("approved", 0) >= before + 2


def test_report_builder_formats(monkeypatch):
    from utils import metrics as m
    from utils import report_builder

    # Ensure a clean counter snapshot for deterministic output
    m.get_all(reset=True)

    monkeypatch.setattr("utils.cache.stats", lambda: {"hit": 10, "miss": 2, "expired": 1})

    def fake_risk(policy):
        return {
            "equity": 1000.0,
            "daily_pnl": 0.0,
            "cumulative_pnl": 0.0,
            "drawdown_pct": 0.0,
            "exposure": 1.0,
        }

    monkeypatch.setattr(report_builder, "_collect_risk_metrics", fake_risk)

    report = report_builder.build_report()
    text = report_builder.format_text(report)
    assert "Funnel" in text and "Cache" in text
