"""Structured logging helpers with standardized prefixes."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Tuple

from utils import metrics

_rate_lock = threading.Lock()
_last_msg: dict[str, float] = {}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_dir = os.path.join(PROJECT_ROOT, "logs")

_PREFIXES = {
    "SCAN",
    "GATE",
    "SCORE",
    "APPROVAL",
    "ORDER",
    "FILL",
    "EXIT",
    "RISK",
    "CACHE",
    "ERROR",
    "REPORT",
}

_ALIAS_PREFIXES = {
    "SIZE": "ORDER",
    "ENTRY": "ORDER",
    "REGIME": "RISK",
    "PENALTY": "SCORE",
    "RECONCILE": "ORDER",
}

_ERROR_MARKERS = ("❌", "⛔")


def _extract_token(text: str) -> Tuple[str, str]:
    parts = text.split(maxsplit=1)
    if not parts:
        return "", ""
    token = parts[0].rstrip(":").upper()
    rest = parts[1] if len(parts) > 1 else ""
    return token, rest


def _looks_like_symbol(value: str) -> bool:
    cleaned = value.strip().upper()
    if not cleaned:
        return False
    normalized = cleaned.replace(".", "").replace("-", "").replace("/", "")
    return normalized.isalnum() and len(normalized) <= 10


def _split_symbol_and_body(segment: str) -> Tuple[str | None, str]:
    if not segment:
        return None, ""
    if ":" in segment:
        candidate, remainder = segment.split(":", 1)
        if _looks_like_symbol(candidate):
            return candidate.strip(), remainder.strip()
    return None, segment.strip()


def _sanitize_event(event: str | None) -> str | None:
    if not event:
        return None
    upper = event.upper()
    return upper if upper in _PREFIXES else None


def _infer_event_from_content(message: str) -> str:
    lower = message.lower()
    if any(marker in message for marker in _ERROR_MARKERS) or "error" in lower:
        return "ERROR"
    if "cache" in lower:
        return "CACHE"
    if "exposure" in lower or "risk" in lower:
        return "RISK"
    return "REPORT"


def _normalize_message(message: str, event_hint: str | None, symbol_hint: str | None) -> Tuple[str, str | None, str]:
    text = str(message).strip()
    token, rest = _extract_token(text)
    event = _sanitize_event(event_hint)
    symbol = symbol_hint.strip() if isinstance(symbol_hint, str) else symbol_hint
    body = text

    if event is None and token:
        if token in _PREFIXES:
            event = token
            sym_candidate, remainder = _split_symbol_and_body(rest)
            if sym_candidate and not symbol:
                symbol = sym_candidate
            body = remainder
        elif token in _ALIAS_PREFIXES:
            event = _ALIAS_PREFIXES[token]
            sym_candidate, remainder = _split_symbol_and_body(rest)
            if sym_candidate and not symbol:
                symbol = sym_candidate
            body = f"{token} {remainder}".strip()

    if event is None:
        event = _infer_event_from_content(text)
        body = text

    if event not in _PREFIXES:
        event = "REPORT"

    body = body.strip()
    return event, symbol, body


def log_event(message, **fields):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "events.log")
    approval_file = os.path.join(log_dir, "approvals.log")

    event_hint = fields.pop("event", None) or fields.pop("event_type", None)
    symbol_hint = fields.pop("symbol", None)

    event, symbol, body = _normalize_message(message, event_hint, symbol_hint)

    header = event
    if symbol:
        header = f"{header} {symbol}"
    formatted = header if not body else f"{header}: {body}" if not body.startswith(":") else f"{header}{body}"

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    log_line = f"[{timestamp}] {formatted}" + (f" {extra}" if extra else "")

    print(log_line, flush=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")
    if event == "APPROVAL":
        with open(approval_file, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")

    if event == "ERROR":
        try:
            metrics.inc("errors")
        except Exception:
            pass


def log_once(key: str, message: str, min_interval_sec: float = 60.0, **fields) -> None:
    """Log ``message`` at most once every ``min_interval_sec`` seconds.

    Parameters
    ----------
    key:
        Identifier for the message to be rate-limited.
    message:
        Text to be logged via :func:`log_event`.
    min_interval_sec:
        Minimum number of seconds between log emissions for the same ``key``.
    **fields:
        Extra structured fields passed through to :func:`log_event`.
    """

    now = time.time()
    with _rate_lock:
        last = _last_msg.get(key, 0.0)
        if now - last < max(min_interval_sec, 0.0):
            return
        _last_msg[key] = now
    log_event(message, **fields)
