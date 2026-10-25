"""Guest State / Power State Health Check Plugin"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check


@register_check("guest_state")
class GuestStateCheck(BaseCheck):
    """Monitors expected running state for VMs and LXCs based on onboot/selection rules."""

    async def run(self, context: CheckContext) -> CheckResult:
        select_rules = self.params.get("select", {})
        onboot_filter = select_rules.get("onboot")
        exclude_list = set(self.params.get("exclude", []))

        loop = asyncio.get_running_loop()
        stopped_guests: List[str] = []
        monitored_count = 0

        for node_name, client in context.clients.items():
            try:
                guests = await loop.run_in_executor(None, client.get_guests)
                for g in guests:
                    vmid = int(g.get("vmid"))
                    spec = f"{node_name}/{vmid}"
                    name = g.get("name", f"guest-{vmid}")
                    gtype = "CT" if g.get("type") == "lxc" else "VM"

                    if spec in exclude_list:
                        continue

                    # Filter by onboot if requested
                    if onboot_filter is not None:
                        cfg = await loop.run_in_executor(None, client.get_guest_config, vmid, g.get("type", "qemu"))
                        onboot_val = bool(cfg.get("onboot", False) or g.get("onboot", False))
                        if onboot_val != bool(onboot_filter):
                            continue

                    monitored_count += 1
                    status = g.get("status", "stopped")
                    if status != "running":
                        stopped_guests.append(f"{gtype} {spec} ({name}) is {status.upper()}")

            except Exception as e:
                return CheckResult(
                    check_id=self.id,
                    status=CheckStatus.UNKNOWN,
                    summary=f"Failed querying guests on {node_name}: {e}",
                )

        if stopped_guests:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"{len(stopped_guests)} expected guest(s) not running: " + "; ".join(stopped_guests),
                details={"stopped_guests": stopped_guests, "total_monitored": monitored_count},
            )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"All {monitored_count} onboot guests are running",
            details={"total_monitored": monitored_count},
        )
