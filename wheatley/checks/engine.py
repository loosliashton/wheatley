"""Wheatley Check Engine - Scheduler, State Machine, Deduplication & Alerting"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel, Field

from wheatley.audit import AuditLogger
from wheatley.checks.base import (
    BaseCheck,
    CheckContext,
    CheckResult,
    CheckStatus,
    get_check_class,
)
import wheatley.checks.builtin  # noqa: F401 - ensure built-ins are registered
from wheatley.config import WheatleyConfig
from wheatley.tools.proxmox import ProxmoxClient

logger = logging.getLogger("wheatley.checks.engine")


class CheckState(BaseModel):
    check_id: str
    last_status: CheckStatus = CheckStatus.OK
    last_result: Optional[CheckResult] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_alert_time: Optional[datetime] = None
    muted_until: Optional[datetime] = None

    @property
    def is_muted(self) -> bool:
        if not self.muted_until:
            return False
        return datetime.now(timezone.utc) < self.muted_until


class CheckEngine:
    """Schedules, executes, and tracks states of all configured health checks."""

    def __init__(
        self,
        config: WheatleyConfig,
        audit: AuditLogger,
        alert_sink: Optional[Callable[[str, Optional[Dict[str, Any]]], Coroutine[Any, Any, None]]] = None,
    ):
        self.config = config
        self.audit = audit
        self.alert_sink = alert_sink
        self.scheduler = AsyncIOScheduler()
        self.states: Dict[str, CheckState] = {}
        self.checks: Dict[str, BaseCheck] = {}

        # Initialize Proxmox clients and context
        self.clients: Dict[str, ProxmoxClient] = {}
        for node_cfg in config.nodes:
            if "xxxx" not in (node_cfg.tokens.monitor or ""):
                try:
                    self.clients[node_cfg.name] = ProxmoxClient(node_cfg)
                except Exception as e:
                    logger.warning("Could not create Proxmox client for %s: %s", node_cfg.name, e)

        self.context = CheckContext(config, self.clients)
        self._initialize_checks()

    def _initialize_checks(self) -> None:
        """Instantiate configured checks."""
        for chk_cfg in self.config.checks:
            if not chk_cfg.enabled:
                continue

            cls = get_check_class(chk_cfg.type)
            if not cls:
                logger.warning("Unknown check type '%s' for check '%s'", chk_cfg.type, chk_cfg.id)
                continue

            instance = cls(chk_cfg)
            self.checks[chk_cfg.id] = instance
            self.states[chk_cfg.id] = CheckState(check_id=chk_cfg.id)

    def mute_check(self, check_id: str, minutes: int = 60) -> Optional[datetime]:
        """Temporarily mute alerts for a check."""
        state = self.states.get(check_id)
        if not state:
            return None
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        state.muted_until = until
        self.audit.log_event("CHECK", f"engine", f"mute:{check_id}", "MUTED", details={"minutes": minutes, "until": until.isoformat()})
        return until

    def unmute_check(self, check_id: str) -> bool:
        """Unmute alerts for a check."""
        state = self.states.get(check_id)
        if not state:
            return False
        state.muted_until = None
        self.audit.log_event("CHECK", f"engine", f"unmute:{check_id}", "UNMUTED")
        return True

    def get_check_states(self) -> List[Dict[str, Any]]:
        """Return snapshot of all configured checks and their current state."""
        res = []
        for check_id, check in self.checks.items():
            state = self.states.get(check_id)
            res.append({
                "id": check_id,
                "type": check.type,
                "status": state.last_status.value if state else "UNKNOWN",
                "is_muted": state.is_muted if state else False,
                "muted_until": state.muted_until if state else None,
                "consecutive_failures": state.consecutive_failures if state else 0,
                "last_result": state.last_result if state else None,
            })
        return res


    async def execute_check(self, check_id: str) -> CheckResult:
        """Execute a single check and process state transitions."""
        check = self.checks.get(check_id)
        state = self.states.get(check_id)
        if not check or not state:
            return CheckResult(
                check_id=check_id,
                status=CheckStatus.UNKNOWN,
                summary=f"Check '{check_id}' not found",
            )

        try:
            result = await check.run(self.context)
        except Exception as e:
            logger.exception("Check '%s' raised unexpected exception: %s", check_id, e)
            result = CheckResult(
                check_id=check_id,
                status=CheckStatus.CRIT,
                summary=f"Check exception: {e}",
            )

        prev_status = state.last_status
        curr_status = result.status
        state.last_result = result
        state.last_status = curr_status

        # Update streak counts
        if curr_status == CheckStatus.OK:
            state.consecutive_successes += 1
            state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1
            state.consecutive_successes = 0

        # State Transition: OK -> CRIT/WARN (Alert)
        if prev_status == CheckStatus.OK and curr_status in (CheckStatus.CRIT, CheckStatus.WARN):
            self.audit.log_event(
                "ALERT",
                "check_engine",
                f"alert:{check_id}",
                curr_status.value,
                target=check_id,
                details={"summary": result.summary, "details": result.details},
            )
            if not state.is_muted and self.alert_sink:
                icon = "🚨 *CRITICAL ALERT*" if curr_status == CheckStatus.CRIT else "⚠️ *WARNING ALERT*"
                msg = (
                    f"{icon}\n"
                    f"• *Check:* `{check_id}` ({check.type})\n"
                    f"• *Status:* `{curr_status.value}`\n"
                    f"• *Issue:* {result.summary}\n"
                    f"• *Time:* `{result.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}`"
                )
                extra = {"check_id": check_id, "type": "alert"}
                await self.alert_sink(msg, extra)

        # State Transition: CRIT/WARN -> OK (Resolved)
        elif prev_status in (CheckStatus.CRIT, CheckStatus.WARN) and curr_status == CheckStatus.OK:
            self.audit.log_event(
                "ALERT",
                "check_engine",
                f"resolve:{check_id}",
                "RESOLVED",
                target=check_id,
                details={"summary": result.summary},
            )
            if self.alert_sink:
                msg = (
                    f"🟢 *RESOLVED: {check_id}*\n"
                    f"• *Service:* `{check_id}` has returned to normal.\n"
                    f"• *Details:* {result.summary}\n"
                    f"• *Time:* `{result.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}`"
                )
                extra = {"check_id": check_id, "type": "resolved"}
                await self.alert_sink(msg, extra)

        return result

    def start(self) -> None:
        """Start scheduling and running all configured checks."""
        logger.info("Starting check engine scheduler with %d checks...", len(self.checks))
        for check_id, check in self.checks.items():
            interval = max(check.config.interval_seconds, 10)
            self.scheduler.add_job(
                self.execute_check,
                "interval",
                seconds=interval,
                args=[check_id],
                id=f"job_{check_id}",
                replace_existing=True,
            )
        self.scheduler.start()

    def stop(self) -> None:
        """Stop the check engine scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Check engine scheduler stopped.")
