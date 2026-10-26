"""Wheatley Token Usage Tracker (LLM-Agnostic, No Billing Blockers)"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import sqlite3
from typing import Optional, Tuple

from wheatley.audit import AuditLogger

logger = logging.getLogger("wheatley.llm.usage")


class UsageTracker:
    """Tracks token consumption and request telemetry without imposing artificial billing caps or warnings."""

    def __init__(self, audit: AuditLogger):
        self.audit = audit

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.audit.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def record_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        context: str = "",
    ) -> None:
        """Record token consumption in SQLite and log audit event."""
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO budget_usage (timestamp, model, input_tokens, output_tokens, cost_usd, context)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_iso, model, input_tokens, output_tokens, 0.0, context),
            )
            conn.commit()

        # Log to general audit log
        self.audit.log_event(
            event_type="LLM",
            source="llm",
            action=f"call:{model}",
            status="SUCCESS",
            target=context or None,
            details={
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )

    def get_daily_tokens(self, date_str: Optional[str] = None) -> Tuple[int, int]:
        """Get (input_tokens, output_tokens) for a given day (defaults to today UTC)."""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) as inp, COALESCE(SUM(output_tokens), 0) as outp
                FROM budget_usage
                WHERE timestamp LIKE ?
                """,
                (f"{date_str}%",),
            )
            row = cursor.fetchone()
            return (int(row["inp"]), int(row["outp"])) if row else (0, 0)

    def get_monthly_tokens(self, month_str: Optional[str] = None) -> Tuple[int, int]:
        """Get (input_tokens, output_tokens) for a given month (defaults to current month UTC)."""
        if not month_str:
            month_str = datetime.now(timezone.utc).strftime("%Y-%m")

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) as inp, COALESCE(SUM(output_tokens), 0) as outp
                FROM budget_usage
                WHERE timestamp LIKE ?
                """,
                (f"{month_str}%",),
            )
            row = cursor.fetchone()
            return (int(row["inp"]), int(row["outp"])) if row else (0, 0)

    def get_total_requests(self, month_str: Optional[str] = None) -> int:
        """Get total number of LLM inference requests logged."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if month_str:
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM budget_usage WHERE timestamp LIKE ?",
                    (f"{month_str}%",),
                )
            else:
                cursor.execute("SELECT COUNT(*) as cnt FROM budget_usage")
            row = cursor.fetchone()
            return int(row["cnt"]) if row else 0

    def format_usage_markdown(self, provider: str = "llm", model: str = "default") -> str:
        """Format an informative Markdown telemetry summary for Telegram /usage or CLI."""
        now = datetime.now(timezone.utc)
        current_month = now.strftime("%Y-%m")
        day_inp, day_outp = self.get_daily_tokens()
        month_inp, month_outp = self.get_monthly_tokens()
        month_total = month_inp + month_outp
        day_total = day_inp + day_outp
        req_count = self.get_total_requests(current_month)

        lines = [
            "*LLM Usage & Activity*",
            f"• *Provider:* `{provider}`",
            f"• *Model:* `{model}`",
            f"• *Requests This Month:* `{req_count:,}`",
            f"• *Today's Tokens:* `{day_total:,}` (`{day_inp:,}` in / `{day_outp:,}` out)",
            f"• *Month ({current_month}) Tokens:* `{month_total:,}` (`{month_inp:,}` in / `{month_outp:,}` out)",
            "",
            "ℹ️ _Wheatley operates with no artificial billing caps or execution pauses._",
        ]
        return "\n".join(lines)
