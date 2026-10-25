"""Wheatley Audit & Storage Engine (SQLite)"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class AuditLogger:
    """Manages SQLite audit log, proposals, budget usage, and ledger."""

    def __init__(self, db_path: str | Path = "/var/lib/wheatley/wheatley.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

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
                    details TEXT,
                    cost_usd REAL DEFAULT 0.0
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    target TEXT NOT NULL,
                    command TEXT NOT NULL,
                    reason TEXT,
                    risk TEXT NOT NULL,
                    status TEXT NOT NULL,
                    executed_at REAL,
                    execution_result TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS budget_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    context TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS credit_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    grant_month TEXT NOT NULL UNIQUE,
                    grant_usd REAL NOT NULL,
                    remaining_usd REAL NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
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
        cost_usd: float = 0.0,
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        details_str = json.dumps(details) if isinstance(details, dict) else (details or "")
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO audit_log (timestamp, event_type, source, target, action, status, details, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (now_iso, event_type, source, target, action, status, details_str, cost_usd),
            )
            conn.commit()
            return cursor.lastrowid

    def get_recent_logs(self, limit: int = 15) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, timestamp, event_type, source, target, action, status, details, cost_usd
                FROM audit_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
