"""Proxmox Node Metrics Health Check Plugin"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check


@register_check("node_metrics")
class NodeMetricsCheck(BaseCheck):
    """Monitors CPU and RAM utilization across Proxmox cluster nodes."""

    async def run(self, context: CheckContext) -> CheckResult:
        nodes_param = self.params.get("nodes", "*")
        if nodes_param == "*":
            target_nodes = list(context.clients.keys())
        elif isinstance(nodes_param, list):
            target_nodes = nodes_param
        else:
            target_nodes = [str(nodes_param)]

        cpu_crit = float(self.params.get("cpu_crit_percent", 90.0))
        mem_crit = float(self.params.get("mem_crit_percent", 90.0))
        cpu_warn = float(self.params.get("cpu_warn_percent", 80.0))
        mem_warn = float(self.params.get("mem_warn_percent", 80.0))

        loop = asyncio.get_running_loop()
        issues: List[str] = []
        metrics: Dict[str, Any] = {}
        worst_status = CheckStatus.OK

        for node_name in target_nodes:
            client = context.get_client(node_name)
            if not client:
                issues.append(f"Node '{node_name}' not configured")
                worst_status = CheckStatus.UNKNOWN
                continue

            try:
                status = await loop.run_in_executor(None, client.get_node_status)
                cpu_pct = round(status.get("cpu", 0) * 100, 1)

                mem_data = status.get("memory", {})
                used = mem_data.get("used", 0)
                total = mem_data.get("total", 1)
                mem_pct = round((used / total) * 100, 1) if total else 0

                metrics[f"{node_name}_cpu_pct"] = cpu_pct
                metrics[f"{node_name}_mem_pct"] = mem_pct

                if cpu_pct >= cpu_crit:
                    issues.append(f"{node_name} CPU critical ({cpu_pct}% >= {cpu_crit}%)")
                    worst_status = CheckStatus.CRIT
                elif cpu_pct >= cpu_warn and worst_status != CheckStatus.CRIT:
                    issues.append(f"{node_name} CPU warning ({cpu_pct}% >= {cpu_warn}%)")
                    worst_status = CheckStatus.WARN

                if mem_pct >= mem_crit:
                    issues.append(f"{node_name} RAM critical ({mem_pct}% >= {mem_crit}%)")
                    worst_status = CheckStatus.CRIT
                elif mem_pct >= mem_warn and worst_status != CheckStatus.CRIT:
                    issues.append(f"{node_name} RAM warning ({mem_pct}% >= {mem_warn}%)")
                    worst_status = CheckStatus.WARN

            except Exception as e:
                issues.append(f"{node_name} error querying metrics: {e}")
                worst_status = CheckStatus.CRIT

        if worst_status != CheckStatus.OK:
            return CheckResult(
                check_id=self.id,
                status=worst_status,
                summary="; ".join(issues),
                metrics=metrics,
                details={"issues": issues},
            )

        summary_parts = [f"{n}: CPU {metrics.get(f'{n}_cpu_pct')}% / RAM {metrics.get(f'{n}_mem_pct')}%" for n in target_nodes]
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary="All nodes healthy (" + ", ".join(summary_parts) + ")",
            metrics=metrics,
        )
