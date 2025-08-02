from utils import daily_risk


def test_register_and_limit(monkeypatch, tmp_path):
    log_file = tmp_path / "daily_pnl_log.csv"
    monkeypatch.setattr(daily_risk, "PNL_LOG_FILE", log_file)
    monkeypatch.setenv("DAILY_RISK_LIMIT", "-100")

    daily_risk.register_trade_pnl("TEST", -30)
    daily_risk.register_trade_pnl("TEST", -80)

    assert daily_risk.get_today_pnl() == -110
    assert daily_risk.is_risk_limit_exceeded()
