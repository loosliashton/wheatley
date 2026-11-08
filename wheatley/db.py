"""Unified SQLite Connection Manager for Wheatley"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sqlite3

logger = logging.getLogger("wheatley.db")


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
            self._ensure_permissions()

    def _ensure_permissions(self) -> None:
        """If running as root and user 'wheatley' exists, ensure /var/lib/wheatley db files are owned by wheatley."""
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            return
        try:
            import pwd
            pw = pwd.getpwnam("wheatley")
        except (KeyError, ImportError):
            return

        wheatley_home = Path(pw.pw_dir).resolve()
        default_dir = Path("/var/lib/wheatley").resolve()
        try:
            resolved_parent = self.db_path.parent.resolve()
        except OSError:
            return

        if not (
            resolved_parent == default_dir
            or default_dir in resolved_parent.parents
            or resolved_parent == wheatley_home
            or wheatley_home in resolved_parent.parents
        ):
            return

        for target in (
            self.db_path.parent,
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if target.exists():
                try:
                    os.chown(target, pw.pw_uid, pw.pw_gid)
                except OSError as e:
                    logger.warning("Failed to chown %s to user 'wheatley': %s", target, e)

    def _configure_pragmas(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        return conn

