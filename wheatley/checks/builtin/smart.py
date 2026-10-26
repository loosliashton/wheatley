"""S.M.A.R.T. Drive Health Check Plugin

Monitors storage drive SMART health status (NVMe, SSD, HDD) via smartctl.
"""

from __future__ import annotations

import logging
from typing import Dict, List
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check

logger = logging.getLogger("wheatley.checks.builtin.smart")


@register_check("smart")
class SmartCheck(BaseCheck):
    """Monitors drive SMART health status on Proxmox VE hosts."""

    async def run(self, context: CheckContext) -> CheckResult:
        if not context.shell:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="ShellExecutor is not configured in CheckContext",
            )

        node = self.params.get("node")
        devices_param = self.params.get("devices", [])
        if isinstance(devices_param, str):
            devices = [devices_param]
        else:
            devices = list(devices_param)

        if not node:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'node' parameter",
            )

        if not devices:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'devices' parameter (e.g. ['/dev/nvme0n1', '/dev/sda'])",
            )

        issues: List[str] = []
        drive_results: Dict[str, str] = {}

        for dev in devices:
            dev_path = dev.strip()
            cmd = f"smartctl -H {dev_path}"
            res = await context.shell.execute_on_host(node, cmd, timeout=15)

            combined_output = (res.stdout + "\n" + res.stderr).lower()
            if "passed" in combined_output or "ok" in combined_output:
                drive_results[dev_path] = "PASSED"
            elif "failed" in combined_output:
                drive_results[dev_path] = "FAILED"
                issues.append(f"{dev_path} SMART health FAILED on {node}")
            else:
                drive_results[dev_path] = f"UNKNOWN (code {res.exit_code})"
                if res.exit_code != 0:
                    issues.append(f"{dev_path} SMART check error: {res.stderr.strip()[:100]}")

        if issues:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary="; ".join(issues),
                details={"node": node, "devices": drive_results, "issues": issues},
            )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"All {len(devices)} drive(s) on {node} reported healthy (PASSED)",
            details={"node": node, "devices": drive_results},
        )
