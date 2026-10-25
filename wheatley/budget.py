"""Wheatley Budget Guardrails & Credit Ledger"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, Optional, Tuple

from wheatley.audit import AuditLogger
from wheatley.config import BudgetConfig

logger = logging.getLogger("wheatley.budget")


class BudgetTracker:
    """Tracks token consumption, calculates costs, and enforces GCP credit guardrails."""

    def __init__(self, config: BudgetConfig, audit: AuditLogger):
        self.config = config
        self.audit = audit
        self._ensure_monthly_grant()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.audit.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_monthly_grant(self) -> None:
        """Ensure current month has a grant row in credit_ledger."""
        now = datetime.now(timezone.utc)
        current_month = now.strftime("%Y-%m")
        created_at = now.timestamp()
        # Expires in 12 months (365 days)
        expires_at = created_at + (365 * 86400)

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO credit_ledger (grant_month, grant_usd, remaining_usd, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (current_month, self.config.monthly_grant_usd, self.config.monthly_grant_usd, created_at, expires_at),
            )
            conn.commit()

    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Compute estimated USD cost based on token counts."""
        cost = (
            (input_tokens / 1_000_000.0) * self.config.input_cost_per_million
            + (output_tokens / 1_000_000.0) * self.config.output_cost_per_million
        )
        return round(cost, 6)

    def record_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        context: str = "",
    ) -> float:
        """Record token consumption in SQLite and return cost in USD."""
        cost_usd = self.calculate_cost(input_tokens, output_tokens)
        now_iso = datetime.now(timezone.utc).isoformat()
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO budget_usage (timestamp, model, input_tokens, output_tokens, cost_usd, context)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_iso, model, input_tokens, output_tokens, cost_usd, context),
            )
            # Deduct from current month grant in ledger
            cursor.execute(
                """
                UPDATE credit_ledger
                SET remaining_usd = MAX(0.0, remaining_usd - ?)
                WHERE grant_month = ?
                """,
                (cost_usd, current_month),
            )
            conn.commit()

        # Log to general audit log
        self.audit.log_event(
            event_type="LLM",
            source="gemini",
            action=f"call:{model}",
            status="SUCCESS",
            target=context or None,
            details={
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            },
            cost_usd=cost_usd,
        )

        return cost_usd

    def get_daily_spend(self, date_str: Optional[str] = None) -> float:
        """Get total USD spend for a given day (defaults to today UTC)."""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0.0) as total
                FROM budget_usage
                WHERE timestamp LIKE ?
                """,
                (f"{date_str}%",),
            )
            row = cursor.fetchone()
            return round(row["total"] if row else 0.0, 4)

    def get_monthly_spend(self, month_str: Optional[str] = None) -> float:
        """Get total USD spend for a given month (defaults to current month UTC)."""
        if not month_str:
            month_str = datetime.now(timezone.utc).strftime("%Y-%m")

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0.0) as total
                FROM budget_usage
                WHERE timestamp LIKE ?
                """,
                (f"{month_str}%",),
            )
            row = cursor.fetchone()
            return round(row["total"] if row else 0.0, 4)

    def get_monthly_tokens(self, month_str: Optional[str] = None) -> Tuple[int, int]:
        """Get total (input_tokens, output_tokens) for month."""
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

    def get_carried_over_bank(self) -> float:
        """Get unexpired carried-over credit from previous grants plus starting bank."""
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        now_ts = datetime.now(timezone.utc).timestamp()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(SUM(remaining_usd), 0.0) as bank
                FROM credit_ledger
                WHERE grant_month != ? AND expires_at > ?
                """,
                (current_month, now_ts),
            )
            row = cursor.fetchone()
            ledger_bank = float(row["bank"]) if row else 0.0

        return round(self.config.starting_bank_usd + ledger_bank, 2)

    def set_starting_bank(self, amount: float) -> None:
        """Set or update starting carried-over credit bank in SQLite."""
        now = datetime.now(timezone.utc)
        created_at = now.timestamp()
        expires_at = created_at + (10 * 365 * 86400)

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO credit_ledger (grant_month, grant_usd, remaining_usd, created_at, expires_at)
                VALUES ('STARTING_BANK', ?, ?, ?, ?)
                ON CONFLICT(grant_month) DO UPDATE SET
                    grant_usd = excluded.grant_usd,
                    remaining_usd = excluded.remaining_usd
                """,
                (amount, amount, created_at, expires_at),
            )
            conn.commit()

        self.audit.log_event(
            "BUDGET",
            "user",
            "set_bank",
            "SUCCESS",
            details={"starting_bank_usd": amount},
        )

    def can_call_llm(self) -> Tuple[bool, str]:
        """Check budget safety guardrails before executing an LLM call."""
        daily_spend = self.get_daily_spend()
        if daily_spend >= self.config.daily_cap_usd:
            return (
                False,
                f"Daily AI budget cap reached (${daily_spend:.2f} >= ${self.config.daily_cap_usd:.2f}/day). "
                "AI reasoning is paused until midnight UTC.",
            )

        monthly_spend = self.get_monthly_spend()
        bank = self.get_carried_over_bank()
        effective_limit = self.config.pause_threshold_usd + bank

        if monthly_spend >= effective_limit:
            return (
                False,
                f"Monthly AI budget limit reached (${monthly_spend:.2f} >= ${effective_limit:.2f}). "
                "AI features are paused. Monitoring, checks, and approvals remain active.",
            )

        return True, "OK"

    def format_usage_markdown(self) -> str:
        """Format a clear Markdown report for Telegram /usage."""
        now = datetime.now(timezone.utc)
        current_month = now.strftime("%Y-%m")
        month_spend = self.get_monthly_spend()
        daily_spend = self.get_daily_spend()
        inp_tokens, outp_tokens = self.get_monthly_tokens()
        bank = self.get_carried_over_bank()

        grant = self.config.monthly_grant_usd
        pct = round((month_spend / grant) * 100, 1) if grant else 0.0

        can_call, status_reason = self.can_call_llm()
        if not can_call:
            status_icon = "⏸️ *PAUSED*"
        elif month_spend >= self.config.warn_threshold_usd:
            status_icon = "⚠️ *WARNING THRESHOLD*"
        else:
            status_icon = "🟢 *ACTIVE*"

        lines = [
            "💳 *GCP Credit & Gemini Budget*",
            f"• *Status:* {status_icon}",
            f"• *Model:* `{self.config.model_flash}`",
            f"• *Month ({current_month}) Spend:* `${month_spend:.4f}` / `${grant:.2f}` ({pct}%)",
            f"• *Today's Spend:* `${daily_spend:.4f}` / `${self.config.daily_cap_usd:.2f}` cap",
            f"• *Carried-over Bank:* `${bank:.2f}`",
            f"• *Tokens This Month:* `{inp_tokens:,}` in / `{outp_tokens:,}` out",
            "",
            "🛡 *Safety Guardrails*",
            f"• *Warn threshold:* `${self.config.warn_threshold_usd:.2f}`",
            f"• *Pause threshold:* `${self.config.pause_threshold_usd:.2f}` (+ bank)",
            f"• *Daily cap:* `${self.config.daily_cap_usd:.2f}`/day",
        ]
        return "\n".join(lines)
