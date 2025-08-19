import os
import json
import threading
import time
from datetime import datetime

class DailySet:
    """Thread-safe set that auto-resets each UTC day and persists to disk.

    Parameters
    ----------
    path: str
        File path used to persist the set between runs.
    autosave_interval: int, optional
        Minimum seconds between automatic saves. Defaults to 5 seconds.
    """
    def __init__(self, path: str, autosave_interval: int = 5):
        self.path = path
        self.autosave_interval = autosave_interval
        self.lock = threading.Lock()
        self._last_save = 0.0
        self._day = datetime.utcnow().date()
        self._set = set()
        self._load_unlocked()

    # internal helpers -------------------------------------------------
    def _load_unlocked(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            day = datetime.fromisoformat(data.get("date", ""))
            symbols = set(data.get("symbols", []))
            if day.date() == datetime.utcnow().date():
                self._day = day.date()
                self._set = symbols
        except Exception:
            # Fresh start when file is missing or corrupted
            self._day = datetime.utcnow().date()
            self._set = set()

    def _save_unlocked(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"date": self._day.isoformat(), "symbols": sorted(self._set)}, f)
        self._last_save = time.time()

    def _reset_if_new_day_unlocked(self) -> bool:
        today = datetime.utcnow().date()
        if self._day != today:
            self._day = today
            self._set.clear()
            return True
        return False

    # public API -------------------------------------------------------
    def reset_if_new_day(self) -> bool:
        with self.lock:
            return self._reset_if_new_day_unlocked()

    def add(self, item: str) -> None:
        with self.lock:
            self._reset_if_new_day_unlocked()
            self._set.add(item)
            if time.time() - self._last_save >= self.autosave_interval:
                self._save_unlocked()

    def clear(self) -> None:
        with self.lock:
            self._set.clear()
            self._save_unlocked()

    def save(self) -> None:
        with self.lock:
            self._save_unlocked()

    def __contains__(self, item: str) -> bool:
        with self.lock:
            self._reset_if_new_day_unlocked()
            return item in self._set

    def __len__(self) -> int:
        with self.lock:
            self._reset_if_new_day_unlocked()
            return len(self._set)

    def __iter__(self):
        with self.lock:
            self._reset_if_new_day_unlocked()
            return iter(self._set.copy())
