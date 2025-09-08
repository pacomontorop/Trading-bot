from utils import logger


def test_log_files_no_duplication(tmp_path, monkeypatch):
    monkeypatch.setattr(logger, "log_dir", tmp_path)
    logger.log_event("ORDER AAPL: placed")
    logger.log_event("APPROVAL AAPL: ok")
    trading = (tmp_path / "trading.log").read_text()
    approvals = (tmp_path / "approvals.log").read_text()
    assert "ORDER AAPL: placed" in trading
    assert "ORDER AAPL: placed" not in approvals
    assert "APPROVAL AAPL: ok" in approvals
    assert "APPROVAL AAPL: ok" not in trading
