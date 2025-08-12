#test_daily_risk.py

import csv
from datetime import datetime, timedelta

from utils import daily_risk


def test_register_and_limit(monkeypatch, tmp_path):
    log_file = tmp_path / "daily_pnl_log.csv"
    monkeypatch.setattr(daily_risk, "PNL_LOG_FILE", log_file)
    monkeypatch.setenv("DAILY_RISK_LIMIT", "-100")

    daily_risk.register_trade_pnl("TEST", -30)
    daily_risk.register_trade_pnl("TEST", -80)

    assert daily_risk.get_today_pnl() == -110
    assert daily_risk.is_risk_limit_exceeded()


def test_equity_snapshot_and_drop(monkeypatch, tmp_path):
    log_file = tmp_path / "equity_log.csv"
    monkeypatch.setattr(daily_risk, "EQUITY_LOG_FILE", log_file)

    class DummyAPI:
        def __init__(self, equity):
            self._equity = equity

        def get_account(self):
            return type("A", (), {"equity": str(self._equity)})()

    # Save snapshot for today
    monkeypatch.setattr(daily_risk, "api", DummyAPI(1000))
    daily_risk.save_equity_snapshot()
    assert log_file.exists()
    with open(log_file, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 2  # header + one entry

    # Prepare log with yesterday's equity
    yesterday = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    with open(log_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "equity"])
        writer.writerow([yesterday, "1000"])

    monkeypatch.setattr(daily_risk, "api", DummyAPI(900))
    assert daily_risk.is_equity_drop_exceeded(5.0)

    monkeypatch.setattr(daily_risk, "api", DummyAPI(980))
    assert not daily_risk.is_equity_drop_exceeded(5.0)


def test_var_and_drawdown(monkeypatch, tmp_path):
    equity_file = tmp_path / "equity_log.csv"
    monkeypatch.setattr(daily_risk, "EQUITY_LOG_FILE", equity_file)

    base = datetime.utcnow().date() - timedelta(days=4)
    with open(equity_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "equity"])
        equities = [100, 95, 97, 96, 94]
        for i, eq in enumerate(equities):
            writer.writerow([(base + timedelta(days=i)).isoformat(), str(eq)])

    assert round(daily_risk.get_max_drawdown(), 2) == -6.0
    var = daily_risk.calculate_var(window=4, confidence=0.95)
    assert round(var * 100, 2) == 4.56
