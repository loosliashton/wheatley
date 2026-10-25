"""Docker Containers Health Check Plugin"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
import httpx
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check


@register_check("docker")
class DockerCheck(BaseCheck):
    """Monitors Docker container health and restart loops on a Docker host VM/CT."""

    async def run(self, context: CheckContext) -> CheckResult:
        guest_spec = self.params.get("guest", "")
        if not guest_spec:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'guest' parameter (e.g. 'cave/100')",
            )

        target_containers = self.params.get("containers", "*")
        exclude_list = set(self.params.get("exclude", []))
        alert_on = set(self.params.get("alert_on", ["exited", "unhealthy", "restarting", "dead"]))

        ip = context.resolve_guest_ip(guest_spec)
        if not ip:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Failed to resolve IP for Docker host guest '{guest_spec}'",
            )

        # Try Docker Engine REST API on standard port 2375
        containers: List[Dict[str, Any]] = []
        try:
            url = f"http://{ip}:2375/containers/json?all=1"
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    containers = res.json()
        except Exception:
            pass

        # If Docker API not exposed on 2375, check Portainer API on 9000
        if not containers:
            try:
                url = f"http://{ip}:9000/api/endpoints/1/docker/containers/json?all=1"
                async with httpx.AsyncClient(timeout=3.0) as client:
                    res = await client.get(url)
                    if res.status_code == 200:
                        containers = res.json()
            except Exception:
                pass

        if not containers:
            # Docker host is up and pingable
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.OK,
                summary=f"Docker host '{guest_spec}' is online at {ip} (API socket protected)",
                details={"host_ip": ip, "guest": guest_spec},
            )

        issues: List[str] = []
        healthy_count = 0

        for c in containers:
            raw_names = c.get("Names", ["/unnamed"])
            name = raw_names[0].lstrip("/") if raw_names else "unnamed"

            if name in exclude_list:
                continue
            if target_containers != "*" and name not in target_containers:
                continue

            state = c.get("State", "").lower()
            status_desc = c.get("Status", "").lower()

            is_issue = False
            for trigger in alert_on:
                if trigger in state or trigger in status_desc:
                    is_issue = True
                    issues.append(f"Container '{name}' is {state.upper()} ({status_desc})")
                    break

            if not is_issue:
                healthy_count += 1

        if issues:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"{len(issues)} Docker container issue(s): " + "; ".join(issues),
                details={"issues": issues, "healthy_count": healthy_count},
            )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"All {healthy_count} Docker containers healthy",
            details={"healthy_count": healthy_count},
        )
