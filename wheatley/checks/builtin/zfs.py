"""ZFS Storage Pool Health Check Plugin

Monitors ZFS pool health, checksum errors, degradation, and faulted disks via zpool status.
"""

from __future__ import annotations

import logging
from typing import List
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check

logger = logging.getLogger("wheatley.checks.builtin.zfs")


@register_check("zfs")
class ZfsCheck(BaseCheck):
    """Monitors ZFS pool health across Proxmox VE hosts."""

    async def run(self, context: CheckContext) -> CheckResult:
        if not context.shell:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="ShellExecutor is not configured in CheckContext",
            )

        nodes_param = self.params.get("nodes") or self.params.get("node") or "*"
        pool = self.params.get("pool") or self.params.get("pools")
        if pool == "*" or isinstance(pool, list):
            pool = " ".join(str(p) for p in pool) if isinstance(pool, list) else None

        if nodes_param == "*":
            target_nodes = [n.name for n in context.config.nodes]
        elif isinstance(nodes_param, list):
            target_nodes = nodes_param
        else:
            target_nodes = [str(nodes_param)]

        issues: List[str] = []
        healthy_nodes: List[str] = []

        for node_name in target_nodes:
            cmd = f"zpool status -x {pool}" if pool else "zpool status -x"
            res = await context.shell.execute_on_host(node_name, cmd, timeout=15)

            if not res.success:
                # If zpool command failed because zfs is not installed / no pools exist
                if "no pools" in res.stderr.lower() or "not found" in res.stderr.lower():
                    continue
                issues.append(f"{node_name}: zpool status failed: {res.stderr.strip()[:200]}")
                continue

            stdout_text = res.stdout.strip()
            if "all pools are healthy" in stdout_text:
                healthy_nodes.append(node_name)
            elif stdout_text:
                # Any output from 'zpool status -x' other than 'all pools are healthy' indicates an issue
                issues.append(f"{node_name}: {stdout_text[:250]}")
            else:
                healthy_nodes.append(node_name)

        if not healthy_nodes and not issues:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.OK,
                summary="No ZFS pools found on monitored nodes",
                details={"target_nodes": target_nodes},
            )

        if issues:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary="ZFS degradation detected: " + "; ".join(issues),
                details={"issues": issues, "healthy_nodes": healthy_nodes},
            )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"All ZFS pools healthy across {len(healthy_nodes)} node(s)",
            details={"healthy_nodes": healthy_nodes},
        )
