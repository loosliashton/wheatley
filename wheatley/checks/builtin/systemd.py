"""Systemd Service Health Check Plugin

Monitors systemd units (active/failed/inactive) on Proxmox VE hosts or inside guests.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check

logger = logging.getLogger("wheatley.checks.builtin.systemd")


@register_check("systemd")
class SystemdCheck(BaseCheck):
    """Monitors systemd unit states on host nodes or inside guests."""

    async def run(self, context: CheckContext) -> CheckResult:
        if not context.shell:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="ShellExecutor is not configured in CheckContext",
            )

        guest_spec = self.params.get("guest")
        node = self.params.get("node")

        if not guest_spec and not node:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'guest' or 'node' parameter",
            )

        # Parse units to check
        units: List[str] = []
        if "units" in self.params:
            u_param = self.params["units"]
            units = u_param if isinstance(u_param, list) else [u_param]
        elif "unit" in self.params:
            units = [self.params["unit"]]
        elif "services" in self.params:
            s_param = self.params["services"]
            units = s_param if isinstance(s_param, list) else [s_param]

        if not units:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing 'units' or 'services' parameter",
            )

        alert_on = set(self.params.get("alert_on", ["failed", "inactive", "dead"]))
        target_name = guest_spec or node
        issues: List[str] = []
        unit_statuses: Dict[str, str] = {}

        for unit in units:
            unit_name = unit.strip()
            if not unit_name.endswith(".service") and "." not in unit_name:
                unit_name = f"{unit_name}.service"

            cmd = f"systemctl is-active {unit_name}"
            if guest_spec:
                res = await context.shell.execute_guest_spec(guest_spec, cmd, timeout=10)
            else:
                res = await context.shell.execute_on_host(node, cmd, timeout=10)

            status_str = res.stdout.strip().lower() or (res.stderr.strip().lower() if res.stderr else "unknown")
            unit_statuses[unit_name] = status_str

            if status_str in alert_on:
                issues.append(f"{unit_name} is {status_str.upper()}")

        if issues:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Systemd issues on {target_name}: " + "; ".join(issues),
                details={"target": target_name, "issues": issues, "statuses": unit_statuses},
            )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"All {len(units)} systemd unit(s) active on {target_name}",
            details={"target": target_name, "statuses": unit_statuses},
        )
