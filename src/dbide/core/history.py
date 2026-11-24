"""Query history, kept in a small SQLite file in the app directory."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from dbide.core.config import app_dir


class QueryHistory:
    def __init__(self, path: Path | None = None):
        self.path = path or app_dir() / "history.sqlite"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS history ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, connection TEXT,"
            " sql TEXT NOT NULL, ok INTEGER NOT NULL, elapsed REAL, rows INTEGER)"
        )
        self._db.commit()

    def add(self, connection: str, sql: str, ok: bool, elapsed: float, rows: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO history (ts, connection, sql, ok, elapsed, rows) VALUES (?,?,?,?,?,?)",
                (time.time(), connection, sql, int(ok), elapsed, rows),
            )
            self._db.execute("DELETE FROM history WHERE id <= (SELECT MAX(id) - 5000 FROM history)")
            self._db.commit()

    def recent(self, search: str = "", limit: int = 500) -> list[tuple]:
        with self._lock:
            return self._db.execute(
                "SELECT ts, connection, sql, ok, elapsed, rows FROM history "
                "WHERE sql LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{search}%", limit),
            ).fetchall()

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM history")
            self._db.commit()
