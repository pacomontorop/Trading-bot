import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional


def _resolve_log_dir() -> Optional[str]:
    candidates = [
        os.getenv("LOG_DIR"),
        "/var/data/logs",
        "/tmp/logs",
        "./logs",
    ]
    for cand in candidates:
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
        except Exception:
            continue
        else:
            return cand
    return None


def _mk_logger():
    logger = logging.getLogger("approvals")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    logger.addHandler(stream_handler)

    log_dir = _resolve_log_dir()
    log_event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "component": "approvals_logger",
    }

    if log_dir:
        log_path = os.path.join(log_dir, "approvals.log")
        try:
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=5_000_000,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.INFO)
            logger.addHandler(file_handler)
            log_event.update(
                {
                    "event": "file_handler_ready",
                    "path": log_path,
                }
            )
            logger.info(json.dumps(log_event, ensure_ascii=False))
        except Exception as exc:
            log_event.update(
                {
                    "event": "console_only",
                    "reason": str(exc),
                }
            )
            logger.warning(json.dumps(log_event, ensure_ascii=False))
    else:
        log_event.update(
            {
                "event": "console_only",
                "reason": "no_writable_directory",
                "hint": "set LOG_DIR or mount /var/data",
            }
        )
        logger.warning(json.dumps(log_event, ensure_ascii=False))

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
