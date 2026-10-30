"""Proposal Execution & Continuation Orchestrator for Wheatley"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
from typing import Any, Optional
import yaml as pyyaml

from wheatley.approvals import ApprovalStore, Proposal
from wheatley.audit import AuditLogger
from wheatley.config import CheckConfig
from wheatley.tools.config_editor import apply_checks_file_update
from wheatley.tools.shell import ShellExecutor

logger = logging.getLogger("wheatley.orchestrator")


class ProposalOrchestrator:
    """Coordinates proposal execution (CONFIG_UPDATE, SELF_UPDATE, host/guest shell) and LLM continuation prompts."""

    def __init__(
        self,
        approvals: ApprovalStore,
        audit: AuditLogger,
        shell: Optional[ShellExecutor] = None,
        check_engine: Optional[Any] = None,
        agent: Optional[Any] = None,
    ):
        self.approvals = approvals
        self.audit = audit
        self.shell = shell
        self.check_engine = check_engine
        self.agent = agent

    async def schedule_service_restart(self, delay_seconds: float = 2.0) -> None:
        """Schedules a clean daemon exit after a delay so systemd (Restart=always) restarts the service."""
        await asyncio.sleep(delay_seconds)
        try:
            logger.info("Triggering clean process exit (SIGTERM) for systemd service restart...")
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception as e:
            logger.error("Failed to trigger service restart: %s", e)

    async def execute_proposal_command(self, prop: Optional[Proposal]) -> str:
        """Execute an approved proposal and return a formatted status/output message."""
        if not prop:
            return "❌ Proposal details not found."

        if getattr(prop, "proposal_type", "") == "CONFIG_UPDATE":
            try:
                payload = json.loads(prop.command)
                filepath = Path(payload["file"])
                new_yaml = payload["new_yaml"]
                backup_path = apply_checks_file_update(filepath, new_yaml)

                if self.check_engine:
                    raw_data = pyyaml.safe_load(new_yaml)
                    raw_list = raw_data if isinstance(raw_data, list) else raw_data.get("checks", [])
                    new_configs = [CheckConfig.model_validate(c) for c in raw_list]
                    self.check_engine.reload_checks(new_configs)

                return (
                    f"⚙️ [SYSTEM] *Updated `{filepath.name}` successfully.*\n"
                    f"• *Backup saved:* `{backup_path.name}`\n"
                    f"• *Check Engine:* Reloaded with active configuration."
                )
            except Exception as e:
                logger.exception("Failed to apply config update: %s", e)
                return f"⚙️ [SYSTEM] *Failed to apply config update:* `{e}`"

        elif getattr(prop, "proposal_type", "") == "SELF_UPDATE":
            try:
                from wheatley.updater import WheatleyUpdater

                updater = WheatleyUpdater()
                exec_res = await asyncio.to_thread(updater.perform_update, restart_service=False)
                if not exec_res.success:
                    return f"⚙️ [SYSTEM] *Update Failed:* {exec_res.message}"

                asyncio.create_task(self.schedule_service_restart(delay_seconds=2.0))

                return (
                    f"⚙️ [SYSTEM] *Wheatley updated successfully!*\n"
                    f"• Version: `{exec_res.old_commit}` ➡️ `{exec_res.new_commit}`\n"
                    f"• Commits pulled: {len(exec_res.commits_pulled)}\n"
                    f"• Service reload scheduled in 2 seconds."
                )
            except Exception as e:
                logger.exception("Self-update failed: %s", e)
                return f"⚙️ [SYSTEM] *Update error:* `{e}`"

        executor = self.shell or (getattr(self.agent, "shell", None) if self.agent else None)
        if not executor:
            return "⚙️ [SYSTEM] Shell executor not available."

        target = prop.target.strip()
        command = prop.command.strip()
        try:
            if "/" in target:
                parts = target.split("/", 1)
                node = parts[0]
                vmid = int(parts[1])
                res = await executor.execute_in_guest(node, vmid, command, timeout=30)
            else:
                res = await executor.execute_on_host(target, command, timeout=30)

            output = res.stdout.strip() or res.stderr.strip() or "(no output)"
            return (
                f"⚙️ [SYSTEM] *Output on `{target}` (exit code {res.exit_code}):*\n"
                f"```bash\n{output}\n```"
            )
        except Exception as e:
            logger.exception("Failed to execute proposal command: %s", e)
            return f"⚙️ [SYSTEM] *Execution error on `{target}`:* `{e}`"

    def build_continuation_prompt(
        self,
        prop: Proposal,
        decision: str,
        execution_output: Optional[str] = None,
    ) -> str:
        """Build the LLM continuation prompt after a proposal is approved or denied."""
        if decision == "DENY":
            return (
                f"Permission to run command '{prop.command}' on target '{prop.target}' was denied by the user.\n"
                "Conclude your previous inquiry without running or re-requesting this command."
            )

        if decision == "ALLOW_ONCE":
            return (
                f"Command '{prop.command}' on target '{prop.target}' was approved and executed.\n"
                f"Execution output:\n{execution_output}\n\n"
                "Please continue your analysis and answer my previous question based on this output."
            )

        if decision == "ALLOW_ALWAYS":
            return (
                f"Command rule '{prop.pattern}' on target '{prop.target}' was permanently allowed and executed.\n"
                f"Execution output:\n{execution_output}\n\n"
                "Please continue your analysis and answer my previous question based on this output."
            )

        # APPROVE
        if prop.proposal_type == "CONFIG_UPDATE":
            return (
                f"Configuration update for '{prop.target}' was approved and applied.\n"
                f"Execution output:\n{execution_output}\n\n"
                "Please confirm to the user that the configuration has been updated and the check engine reloaded."
            )
        if prop.proposal_type == "SELF_UPDATE":
            return (
                f"Wheatley self-update was approved and executed.\n"
                f"Execution output:\n{execution_output}\n\n"
                "Please confirm the update to the user concisely."
            )
        return (
            f"Action command '{prop.command}' on target '{prop.target}' was approved and executed.\n"
            f"Execution output:\n{execution_output}\n\n"
            "Please confirm the result concisely to the user."
        )

