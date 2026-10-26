"""Approval & Dynamic Allowlist Store for Wheatley

Manages SQLite-persisted action proposals and user-learned allowlist rules.
Features:
1. 15-minute expiration window on all proposals.
2. Single-use resolution (cannot replay or reuse approved proposals).
3. Target-scoped dynamic allowlist rules with strict pattern safety validation.
4. One-time ("Run Once") vs persistent ("Always Allow on Target") approval workflows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
import uuid

from wheatley.tools.allowlist import validate_proposed_pattern

logger = logging.getLogger("wheatley.approvals")


@dataclass
class Proposal:
    id: str
    proposal_type: str  # "ACTION" or "ALLOWLIST_RULE"
    target: str
    command: str
    summary: str
    risk: str
    status: str  # "PENDING", "APPROVED", "DENIED", "EXPIRED"
    created_at: datetime
    expires_at: datetime
    pattern: Optional[str] = None
    executed_at: Optional[datetime] = None
    execution_result: Optional[str] = None

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def is_pending(self) -> bool:
        return self.status == "PENDING" and not self.is_expired


@dataclass
class UserAllowlistRule:
    id: int
    target: str
    pattern: str
    description: Optional[str]
    created_by: Optional[int]
    created_at: datetime


class ApprovalStore:
    """SQLite-backed store for interactive proposals and dynamic allowlist rules."""

    def __init__(self, db_path: str = "/var/lib/wheatley/wheatley.db"):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    proposal_type TEXT NOT NULL DEFAULT 'ACTION',
                    target TEXT NOT NULL,
                    command TEXT NOT NULL,
                    pattern TEXT,
                    summary TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    executed_at TEXT,
                    execution_result TEXT
                )
                """
            )
            # Add missing columns if proposals table pre-existed
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(proposals)")
            cols = {row["name"] for row in cur.fetchall()}
            if "proposal_type" not in cols:
                conn.execute("ALTER TABLE proposals ADD COLUMN proposal_type TEXT NOT NULL DEFAULT 'ACTION'")
            if "pattern" not in cols:
                conn.execute("ALTER TABLE proposals ADD COLUMN pattern TEXT")
            if "summary" not in cols:
                conn.execute("ALTER TABLE proposals ADD COLUMN summary TEXT DEFAULT ''")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_allowlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    description TEXT,
                    created_by INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def create_proposal(
        self,
        proposal_type: str,
        target: str,
        command: str,
        summary: str,
        risk: str = "MEDIUM",
        pattern: Optional[str] = None,
        expires_minutes: int = 15,
    ) -> Proposal:
        """Create a new pending proposal."""
        prop_id = f"prop_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=expires_minutes)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO proposals (
                    id, proposal_type, target, command, pattern, summary, risk, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    prop_id,
                    proposal_type,
                    target,
                    command,
                    pattern,
                    summary,
                    risk,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            conn.commit()

        return Proposal(
            id=prop_id,
            proposal_type=proposal_type,
            target=target,
            command=command,
            pattern=pattern,
            summary=summary,
            risk=risk,
            status="PENDING",
            created_at=now,
            expires_at=expires,
        )

    def get_proposal(self, proposal_id: str) -> Optional[Proposal]:
        """Fetch proposal, automatically marking expired proposals."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
            row = cur.fetchone()
            if not row:
                return None

            expires_at = datetime.fromisoformat(row["expires_at"])
            status = row["status"]

            # Auto-expire if pending and past expiration time
            if status == "PENDING" and datetime.now(timezone.utc) > expires_at:
                conn.execute(
                    "UPDATE proposals SET status = 'EXPIRED' WHERE id = ?", (proposal_id,)
                )
                conn.commit()
                status = "EXPIRED"

            return Proposal(
                id=row["id"],
                proposal_type=row["proposal_type"],
                target=row["target"],
                command=row["command"],
                pattern=row["pattern"],
                summary=row["summary"],
                risk=row["risk"],
                status=status,
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=expires_at,
                executed_at=datetime.fromisoformat(row["executed_at"]) if row["executed_at"] else None,
                execution_result=row["execution_result"],
            )

    def resolve_proposal(
        self,
        proposal_id: str,
        decision: str,  # "APPROVE", "ALLOW_ONCE", "ALLOW_ALWAYS", "DENY"
        user_id: int,
    ) -> Tuple[bool, str]:
        """Resolve a proposal. If ALLOW_ALWAYS, inserts into user_allowlist."""
        prop = self.get_proposal(proposal_id)
        if not prop:
            return False, f"Proposal '{proposal_id}' not found"

        if not prop.is_pending:
            return False, f"Proposal is already {prop.status.lower()}"

        with self._get_connection() as conn:
            if decision == "DENY":
                conn.execute(
                    "UPDATE proposals SET status = 'DENIED' WHERE id = ?", (proposal_id,)
                )
                conn.commit()
                return True, "Proposal denied"

            elif decision in ("APPROVE", "ALLOW_ONCE"):
                conn.execute(
                    "UPDATE proposals SET status = 'APPROVED' WHERE id = ?", (proposal_id,)
                )
                conn.commit()
                return True, "Approved for one-time execution"

            elif decision == "ALLOW_ALWAYS":
                pattern = prop.pattern or prop.command.strip()
                valid, reason = validate_proposed_pattern(pattern)
                if not valid:
                    return False, f"Pattern validation failed: {reason}"

                now = datetime.now(timezone.utc).isoformat()
                # Insert rule into user_allowlist
                conn.execute(
                    """
                    INSERT INTO user_allowlist (target, pattern, description, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (prop.target, pattern, prop.summary, user_id, now),
                )
                conn.execute(
                    "UPDATE proposals SET status = 'APPROVED' WHERE id = ?", (proposal_id,)
                )
                conn.commit()
                return True, f"Rule '{pattern}' saved for target '{prop.target}'"

            else:
                return False, f"Unknown decision: '{decision}'"

    def record_execution(self, proposal_id: str, result_summary: str) -> None:
        """Mark proposal as executed with timestamp and result summary."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE proposals SET executed_at = ?, execution_result = ? WHERE id = ?",
                (now, result_summary[:500], proposal_id),
            )
            conn.commit()

    def get_active_allowlist(self, target: Optional[str] = None) -> List[str]:
        """Fetch all user allowlist patterns applicable to the given target or globally."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            if target:
                cur.execute(
                    "SELECT pattern FROM user_allowlist WHERE target = ? OR target = '*'",
                    (target,),
                )
            else:
                cur.execute("SELECT pattern FROM user_allowlist")
            return [row["pattern"] for row in cur.fetchall()]

    def list_user_rules(self) -> List[UserAllowlistRule]:
        """List all active user-added allowlist rules."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM user_allowlist ORDER BY id ASC")
            rules = []
            for r in cur.fetchall():
                rules.append(
                    UserAllowlistRule(
                        id=r["id"],
                        target=r["target"],
                        pattern=r["pattern"],
                        description=r["description"],
                        created_by=r["created_by"],
                        created_at=datetime.fromisoformat(r["created_at"]),
                    )
                )
            return rules

    def revoke_user_rule(self, rule_id: int) -> bool:
        """Delete a rule from user_allowlist by its ID."""
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM user_allowlist WHERE id = ?", (rule_id,))
            conn.commit()
            return cur.rowcount > 0
