"""Custom Command Health Check Plugin

Executes an allowlisted read-only command on a host or guest and verifies exit code / regex output.
"""

from __future__ import annotations

import logging
import re
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check
from wheatley.tools.allowlist import is_command_allowed

logger = logging.getLogger("wheatley.checks.builtin.command")


@register_check("command")
class CommandCheck(BaseCheck):
    """Executes an allowlisted read-only shell command on host or guest."""

    async def run(self, context: CheckContext) -> CheckResult:
        if not context.shell:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="ShellExecutor is not configured in CheckContext",
            )

        cmd = self.params.get("command", "").strip()
        if not cmd:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'command' parameter",
            )

        # Security check: must satisfy allowlist
        allowed, reason = is_command_allowed(cmd, context.config.allowlist)
        if not allowed:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Security violation: {reason}",
                details={"command": cmd, "reason": reason},
            )

        guest_spec = self.params.get("guest")
        node = self.params.get("node")
        expected_code = int(self.params.get("expected_exit_code", 0))
        output_regex = self.params.get("output_regex")
        fail_on_regex = self.params.get("fail_on_regex")
        timeout = int(self.params.get("timeout_seconds", 15))

        if guest_spec:
            target_name = guest_spec
            res = await context.shell.execute_guest_spec(guest_spec, cmd, timeout=timeout)
        elif node:
            target_name = node
            res = await context.shell.execute_on_host(node, cmd, timeout=timeout)
        else:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'guest' or 'node' parameter",
            )

        combined_output = (res.stdout + "\n" + res.stderr).strip()

        if res.timed_out:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Command timed out after {timeout}s on {target_name}",
                details={"command": cmd, "target": target_name},
            )

        if res.exit_code != expected_code:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Command on {target_name} exited with code {res.exit_code} (expected {expected_code})",
                details={
                    "command": cmd,
                    "target": target_name,
                    "exit_code": res.exit_code,
                    "stdout": res.stdout[:500],
                    "stderr": res.stderr[:500],
                },
            )

        if output_regex:
            if not re.search(output_regex, combined_output):
                return CheckResult(
                    check_id=self.id,
                    status=CheckStatus.CRIT,
                    summary=f"Command output on {target_name} did not match expected regex '{output_regex}'",
                    details={"command": cmd, "output": combined_output[:500]},
                )

        if fail_on_regex:
            if re.search(fail_on_regex, combined_output):
                return CheckResult(
                    check_id=self.id,
                    status=CheckStatus.CRIT,
                    summary=f"Command output on {target_name} matched failure regex '{fail_on_regex}'",
                    details={"command": cmd, "output": combined_output[:500]},
                )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"Command succeeded on {target_name}",
            details={"command": cmd, "target": target_name, "exit_code": res.exit_code},
        )
