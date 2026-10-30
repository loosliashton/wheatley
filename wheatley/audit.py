"""Wheatley Audit Storage Engine (SQLite)"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

from wheatley.db import SQLiteDatabase


class AuditLogger:
    """Manages SQLite audit log."""

    def __init__(self, db_path: str | Path | SQLiteDatabase = "/var/lib/wheatley/wheatley.db"):
        self.db = SQLiteDatabase(db_path)
        self.db_path = self.db.db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        return self.db.connect()

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    target TEXT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);
                """
            )
            conn.commit()

    def log_event(
        self,
        event_type: str,
        source: str,
        action: str,
        status: str = "INFO",
        target: Optional[str] = None,
        details: Optional[Dict[str, Any] | str] = None,
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        details_str = json.dumps(details, default=str) if isinstance(details, (dict, list)) else (details or "")
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO audit_log (timestamp, event_type, source, target, action, status, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now_iso, event_type, source, target, action, status, details_str),
            )
            conn.commit()
            return cursor.lastrowid

    def get_recent_logs(self, limit: int = 15) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, timestamp, event_type, source, target, action, status, details
                FROM audit_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
