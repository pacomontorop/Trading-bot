import os
import json
from datetime import date
from threading import Lock
from typing import Set, Dict, Any

from config import USE_REDIS

try:  # pragma: no cover
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None

# ---------------------------------------------------------------------------
# Daily evaluation/execution helpers (legacy behaviour kept for compatibility)
# ---------------------------------------------------------------------------

_lock = Lock()
_state_date: str | None = None
_evaluated: Set[str] = set()
_executed: Set[str] = set()

if USE_REDIS and redis is not None:  # pragma: no cover
    _redis = redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)
else:  # pragma: no cover
    _redis = None


def _json_path() -> str:
    return os.path.join("data", f"state_{date.today():%Y%m%d}.json")


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
    if _redis is not None:  # pragma: no cover
        return
    global _state_date
    today = date.today().isoformat()
    if _state_date != today:
        _load_json()


def already_evaluated_today(symbol: str) -> bool:
    with _lock:
        if _redis is not None:  # pragma: no cover
            return _redis.exists(f"eval:{symbol}") == 1
        _ensure_state()
        return symbol in _evaluated


def mark_evaluated(symbol: str) -> None:
    with _lock:
        if _redis is not None:  # pragma: no cover
            _redis.setex(f"eval:{symbol}", 24 * 3600, 1)
            return
        _ensure_state()
        _evaluated.add(symbol)
        _dump_json()


def already_executed_today(symbol: str) -> bool:
    with _lock:
        if _redis is not None:  # pragma: no cover
            return _redis.exists(f"exec:{symbol}") == 1
        _ensure_state()
        return symbol in _executed


def mark_executed(symbol: str) -> None:
    with _lock:
        if _redis is not None:  # pragma: no cover
            _redis.setex(f"exec:{symbol}", 24 * 3600, 1)
            return
        _ensure_state()
        _executed.add(symbol)
        _dump_json()


# ---------------------------------------------------------------------------
# Persistent trade state (open orders/positions, executed symbols)
# ---------------------------------------------------------------------------

_state_file = os.path.join("data", "state_manager.json")
_state_lock = Lock()
_persistent: Dict[str, Any] = {
    "open_orders": {},
    "open_positions": {},
    "executed_symbols": [],
}


def _load_persistent() -> None:
    if os.path.exists(_state_file):
        try:
            with open(_state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            _persistent.update({
                "open_orders": data.get("open_orders", {}),
                "open_positions": data.get("open_positions", {}),
                "executed_symbols": data.get("executed_symbols", []),
            })
        except Exception:
            pass


def _persist() -> None:
    os.makedirs(os.path.dirname(_state_file), exist_ok=True)
    with open(_state_file, "w", encoding="utf-8") as f:
        json.dump(_persistent, f)


_load_persistent()


class StateManager:
    """Thread-safe persistence for trading state."""

    @classmethod
    def load_open_positions(cls) -> Set[str]:
        with _state_lock:
            return set(_persistent["open_positions"].keys())

    @classmethod
    def add_open_position(cls, symbol: str, coid: str | None = None,
                          qty: float | int | None = None,
                          avg_price: float | None = None) -> None:
        with _state_lock:
            _persistent["open_positions"][symbol] = {
                "coid": coid,
                "qty": qty,
                "avg": avg_price,
            }
            _persist()

    @classmethod
    def remove_open_position(cls, symbol: str) -> None:
        with _state_lock:
            _persistent["open_positions"].pop(symbol, None)
            _persist()

    @classmethod
    def replace_open_positions(cls, symbols: Dict[str, Any]) -> None:
        with _state_lock:
            _persistent["open_positions"] = dict(symbols)
            _persist()

    # --- New atomic helpers ---
    @classmethod
    def add_open_order(cls, symbol: str, coid: str) -> None:
        with _state_lock:
            _persistent["open_orders"][symbol] = coid
            _persist()

    @classmethod
    def remove_open_order(cls, symbol: str, coid: str | None = None) -> None:
        with _state_lock:
            if symbol in _persistent["open_orders"] and (
                coid is None or _persistent["open_orders"][symbol] == coid
            ):
                _persistent["open_orders"].pop(symbol, None)
                _persist()

    @classmethod
    def add_open_position_detailed(
        cls, symbol: str, coid: str, qty: float, avg_price: float
    ) -> None:
        cls.add_open_position(symbol, coid, qty, avg_price)

    @classmethod
    def add_executed_symbol(cls, symbol: str) -> None:
        with _state_lock:
            if symbol not in _persistent["executed_symbols"]:
                _persistent["executed_symbols"].append(symbol)
                _persist()

    @classmethod
    def replace_open_orders(cls, mapping: Dict[str, str]) -> None:
        with _state_lock:
            _persistent["open_orders"] = dict(mapping)
            _persist()

    @classmethod
    def get_open_orders(cls) -> Dict[str, str]:
        with _state_lock:
            return dict(_persistent["open_orders"])

    @classmethod
    def get_open_positions(cls) -> Dict[str, Any]:
        with _state_lock:
            return dict(_persistent["open_positions"])

    @classmethod
    def get_executed_symbols(cls) -> Set[str]:
        with _state_lock:
            return set(_persistent["executed_symbols"])

    @classmethod
    def clear(cls) -> None:
        with _state_lock:
            _persistent["open_orders"].clear()
            _persistent["open_positions"].clear()
            _persistent["executed_symbols"].clear()
            _persist()
