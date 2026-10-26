"""LVM Thin Pool Health & Capacity Check Plugin

Monitors data and metadata utilization for LVM Thin storage pools on Proxmox VE hosts.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check

logger = logging.getLogger("wheatley.checks.builtin.lvm_thin")


@register_check("lvm_thin")
class LvmThinCheck(BaseCheck):
    """Monitors LVM Thin pool capacity and alerts before metadata or data exhaustion."""

    async def run(self, context: CheckContext) -> CheckResult:
        warn_pct = float(self.params.get("warn_percent", 80.0))
        crit_pct = float(self.params.get("crit_percent", 90.0))
        target_pool = self.params.get("pool")
        nodes_param = self.params.get("nodes", "*")

        # Determine target nodes
        if nodes_param == "*":
            target_nodes = [n.name for n in context.config.nodes]
        elif isinstance(nodes_param, list):
            target_nodes = nodes_param
        else:
            target_nodes = [str(nodes_param)]

        issues: List[str] = []
        pools_checked: List[Dict[str, Any]] = []

        for node_name in target_nodes:
            # 1. Try pvesm status via shell executor
            parsed_from_shell = False
            if context.shell:
                res = await context.shell.execute_on_host(node_name, "pvesm status", timeout=10)
                if res.success and res.stdout:
                    lines = res.stdout.strip().splitlines()
                    for line in lines[1:]:  # Skip header
                        parts = line.split()
                        if len(parts) >= 7:
                            name, st_type, status, total, used, avail, pct_str = parts[:7]
                            if st_type.lower() == "lvmthin":
                                if target_pool and name != target_pool:
                                    continue
                                try:
                                    usage_val = float(pct_str.rstrip("%"))
                                except ValueError:
                                    continue

                                pools_checked.append({
                                    "node": node_name,
                                    "pool": name,
                                    "type": st_type,
                                    "used_percent": usage_val,
                                    "total_kib": total,
                                    "used_kib": used,
                                })
                                parsed_from_shell = True

                                if usage_val >= crit_pct:
                                    issues.append(f"{node_name}/{name} thin pool at {usage_val:.1f}% capacity (CRIT >= {crit_pct}%)")
                                elif usage_val >= warn_pct:
                                    issues.append(f"{node_name}/{name} thin pool at {usage_val:.1f}% capacity (WARN >= {warn_pct}%)")

            # 2. Fallback to Proxmox API if shell didn't find pools
            if not parsed_from_shell:
                client = context.get_client(node_name)
                if client:
                    try:
                        storage_list = client.get_storage()
                        for s in storage_list:
                            st_type = str(s.get("type", "")).lower()
                            s_name = s.get("storage", "")
                            if st_type == "lvmthin":
                                if target_pool and s_name != target_pool:
                                    continue
                                total = s.get("total", 0)
                                used = s.get("used", 0)
                                usage_val = (used / total * 100) if total > 0 else 0

                                pools_checked.append({
                                    "node": node_name,
                                    "pool": s_name,
                                    "type": st_type,
                                    "used_percent": round(usage_val, 1),
                                })

                                if usage_val >= crit_pct:
                                    issues.append(f"{node_name}/{s_name} thin pool at {usage_val:.1f}% (CRIT >= {crit_pct}%)")
                                elif usage_val >= warn_pct:
                                    issues.append(f"{node_name}/{s_name} thin pool at {usage_val:.1f}% (WARN >= {warn_pct}%)")
                    except Exception as e:
                        logger.debug("Failed to query storage via API for %s: %s", node_name, e)

        if not pools_checked:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.OK,
                summary="No LVM Thin storage pools found on monitored nodes",
                details={"target_nodes": target_nodes},
            )

        if issues:
            worst_status = CheckStatus.CRIT if any("CRIT" in iss for iss in issues) else CheckStatus.WARN
            return CheckResult(
                check_id=self.id,
                status=worst_status,
                summary="; ".join(issues),
                details={"pools": pools_checked, "issues": issues},
            )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"All {len(pools_checked)} LVM Thin pool(s) within normal limits (<{warn_pct}%)",
            details={"pools": pools_checked},
        )
