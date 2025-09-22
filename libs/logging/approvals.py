import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler


def _mk_logger():
    logger = logging.getLogger("approvals")
    if logger.handlers:
        return logger

    log_dir = os.getenv("LOG_DIR", "/data/logs")
    os.makedirs(log_dir, exist_ok=True)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "approvals.log"),
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


_log = _mk_logger()


def approvals_log(ticker, decision, reason, score=None, signals_active=None, **extras):
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "decision": decision,
        "reason": reason,
        "score": score,
        "signals_active": signals_active,
        **extras,
    }
    _log.info(json.dumps(payload, ensure_ascii=False))
