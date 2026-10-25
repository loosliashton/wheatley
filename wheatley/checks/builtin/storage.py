"""Proxmox Storage Pools Health Check Plugin"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check


@register_check("storage")
class StorageCheck(BaseCheck):
    """Monitors Proxmox storage pool capacity and health."""

    async def run(self, context: CheckContext) -> CheckResult:
        nodes_param = self.params.get("nodes", "*")
        if nodes_param == "*":
            target_nodes = list(context.clients.keys())
        elif isinstance(nodes_param, list):
            target_nodes = nodes_param
        else:
            target_nodes = [str(nodes_param)]

        warn_percent = float(self.params.get("warn_percent", 85.0))
        crit_percent = float(self.params.get("crit_percent", 95.0))

        loop = asyncio.get_running_loop()
        issues: List[str] = []
        metrics: Dict[str, Any] = {}
        worst_status = CheckStatus.OK
        pools_checked = 0

        for node_name in target_nodes:
            client = context.get_client(node_name)
            if not client:
                continue

            try:
                pools = await loop.run_in_executor(None, client.get_storage)
                for pool in pools:
                    # Ignore inactive or zero-sized storage
                    total = pool.get("total", 0)
                    if not total or pool.get("active") == 0:
                        continue

                    pools_checked += 1
                    storage_name = pool.get("storage", "unknown")
                    used = pool.get("used", 0)
                    pct = round((used / total) * 100, 1)
                    key = f"{node_name}_{storage_name}_pct"
                    metrics[key] = pct

                    if pct >= crit_percent:
                        issues.append(f"{node_name}/{storage_name} critical ({pct}% >= {crit_percent}%)")
                        worst_status = CheckStatus.CRIT
                    elif pct >= warn_percent and worst_status != CheckStatus.CRIT:
                        issues.append(f"{node_name}/{storage_name} warning ({pct}% >= {warn_percent}%)")
                        worst_status = CheckStatus.WARN

            except Exception as e:
                issues.append(f"{node_name} error querying storage: {e}")
                worst_status = CheckStatus.CRIT

        if worst_status != CheckStatus.OK:
            return CheckResult(
                check_id=self.id,
                status=worst_status,
                summary="; ".join(issues),
                metrics=metrics,
                details={"issues": issues},
            )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"All {pools_checked} storage pools within normal limits",
            metrics=metrics,
        )
