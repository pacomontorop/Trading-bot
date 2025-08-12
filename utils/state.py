import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Set

class StateManager:
    """Persist simple bot state like open positions using SQLite."""
    def __init__(self, db_path: str = "data/state.db"):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS open_positions(symbol TEXT PRIMARY KEY)"
            )

    def load_open_positions(self) -> Set[str]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT symbol FROM open_positions").fetchall()
            return {r[0] for r in rows}

    def add_open_position(self, symbol: str) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO open_positions(symbol) VALUES (?)", (symbol,)
            )

    def remove_open_position(self, symbol: str) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))

    def replace_open_positions(self, symbols: Iterable[str]) -> None:
        symbols = set(symbols)
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM open_positions")
            conn.executemany(
                "INSERT INTO open_positions(symbol) VALUES (?)",
                [(s,) for s in symbols],
            )
