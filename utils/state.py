import os
import json
from datetime import date
from threading import Lock
from typing import Set
from config import USE_REDIS

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None

_lock = Lock()
_state_date: str | None = None
_evaluated: Set[str] = set()
_executed: Set[str] = set()
_open_positions: Set[str] = set()

if USE_REDIS and redis is not None:
    _redis = redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)
else:
    _redis = None


class StateManager:
    """Backward compatible placeholder for previous API."""

    def load_open_positions(self) -> Set[str]:
        with _lock:
            if _redis is not None:
                return set(_redis.smembers("open_positions"))
            _load_open_positions_file()
            return set(_open_positions)

    def add_open_position(self, symbol: str) -> None:
        with _lock:
            if _redis is not None:
                _redis.sadd("open_positions", symbol)
                return
            _open_positions.add(symbol)
            _dump_open_positions_file()

    def remove_open_position(self, symbol: str) -> None:
        with _lock:
            if _redis is not None:
                _redis.srem("open_positions", symbol)
                return
            _open_positions.discard(symbol)
            _dump_open_positions_file()

    def replace_open_positions(self, symbols: Set[str]) -> None:
        with _lock:
            if _redis is not None:
                pipe = _redis.pipeline()
                pipe.delete("open_positions")
                if symbols:
                    pipe.sadd("open_positions", *symbols)
                pipe.execute()
                return
            global _open_positions
            _open_positions = set(symbols)
            _dump_open_positions_file()


def _json_path() -> str:
    return os.path.join("data", f"state_{date.today():%Y%m%d}.json")


def _open_positions_path() -> str:
    return os.path.join("data", "open_positions.json")


def _load_open_positions_file() -> None:
    global _open_positions
    path = _open_positions_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _open_positions = set(data)
        except Exception:
            _open_positions = set()
    else:
        _open_positions = set()


def _dump_open_positions_file() -> None:
    path = _open_positions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(_open_positions), f)


def _load_json() -> None:
    global _state_date, _evaluated, _executed
    _state_date = date.today().isoformat()
    _evaluated = set()
    _executed = set()
    path = _json_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _evaluated = set(data.get("evaluated", []))
            _executed = set(data.get("executed", []))
        except Exception:
            pass


def _dump_json() -> None:
    path = _json_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"evaluated": list(_evaluated), "executed": list(_executed)}, f)


def _ensure_state() -> None:
    if _redis is not None:
        return
    global _state_date
    today = date.today().isoformat()
    if _state_date != today:
        _load_json()


def already_evaluated_today(symbol: str) -> bool:
    with _lock:
        if _redis is not None:
            return _redis.exists(f"eval:{symbol}") == 1
        _ensure_state()
        return symbol in _evaluated


def mark_evaluated(symbol: str) -> None:
    with _lock:
        if _redis is not None:
            _redis.setex(f"eval:{symbol}", 24 * 3600, 1)
            return
        _ensure_state()
        _evaluated.add(symbol)
        _dump_json()


def already_executed_today(symbol: str) -> bool:
    with _lock:
        if _redis is not None:
            return _redis.exists(f"exec:{symbol}") == 1
        _ensure_state()
        return symbol in _executed


def mark_executed(symbol: str) -> None:
    with _lock:
        if _redis is not None:
            _redis.setex(f"exec:{symbol}", 24 * 3600, 1)
            return
        _ensure_state()
        _executed.add(symbol)
        _dump_json()
