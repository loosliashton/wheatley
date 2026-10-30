"""Unified SQLite Connection Manager for Wheatley"""

from __future__ import annotations

from pathlib import Path
import sqlite3


class SQLiteDatabase:
    """Manages SQLite connection configuration (WAL mode, busy timeout, Row factory)."""

    def __init__(
        self,
        db_path: str | Path = "/var/lib/wheatley/wheatley.db",
        timeout: float = 15.0,
    ):
        if isinstance(db_path, SQLiteDatabase):
            self.db_path = db_path.db_path
            self.timeout = db_path.timeout
        else:
            self.db_path = Path(db_path)
            self.timeout = timeout
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._configure_pragmas()

    def _configure_pragmas(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        return conn

