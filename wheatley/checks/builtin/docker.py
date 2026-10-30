"""Docker Containers Health Check Plugin

Monitors container status and restart loops on Docker hosts (VMs or LXCs)
using native guest execution (qm guest exec / pct exec) with fallback to Docker REST API.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List
import httpx
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check

logger = logging.getLogger("wheatley.checks.builtin.docker")


@register_check("docker")
class DockerCheck(BaseCheck):
    """Monitors Docker container health and restart loops on a Docker host VM/CT."""

    async def run(self, context: CheckContext) -> CheckResult:
        guest_spec = self.params.get("guest", "")
        if not guest_spec:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'guest' parameter (e.g. 'pve-node-01/100')",
            )

        target_containers = self.params.get("containers", "*")
        exclude_list = set(self.params.get("exclude", []))
        alert_on = set(self.params.get("alert_on", ["exited", "unhealthy", "restarting", "dead"]))
        ignore_exited_0 = bool(self.params.get("ignore_exited_0", True))

        containers: List[Dict[str, Any]] = []
        exec_error: str = ""

        # 1. Primary: Native Guest Execution via Proxmox Host
        if context.shell:
            try:
                cmd = 'docker ps -a --format "{{json .}}"'
                shell_res = await context.shell.execute_guest_spec(guest_spec, cmd, timeout=15)
                if shell_res.success and shell_res.stdout.strip():
                    for line in shell_res.stdout.strip().splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            parsed = json.loads(line)
                            containers.append(parsed)
                        except json.JSONDecodeError:
                            continue
                elif not shell_res.success:
                    exec_error = shell_res.stderr.strip() or f"exit code {shell_res.exit_code}"
            except Exception as e:
                exec_error = str(e)
                logger.debug("Native guest docker execution failed for %s: %s", guest_spec, e)

        # 2. Fallback: Direct TCP / Portainer API if native execution did not return containers
        if not containers:
            ip = context.resolve_guest_ip(guest_spec)
            if ip:
                try:
                    url = f"http://{ip}:2375/containers/json?all=1"
                    async with httpx.AsyncClient(timeout=3.0) as client:
                        res = await client.get(url)
                        if res.status_code == 200:
                            containers = res.json()
                except Exception:
                    pass

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
            if exec_error:
                return CheckResult(
                    check_id=self.id,
                    status=CheckStatus.CRIT,
                    summary=f"Docker check failed on '{guest_spec}': {exec_error}",
                    details={"guest": guest_spec, "error": exec_error},
                )
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.OK,
                summary=f"Docker host '{guest_spec}' is online (no containers reported)",
                details={"guest": guest_spec},
            )

        issues: List[str] = []
        healthy_count = 0

        for c in containers:
            # Handle both Docker CLI json format (Names: "foo") and REST API format (Names: ["/foo"])
            raw_names = c.get("Names")
            if isinstance(raw_names, list) and raw_names:
                name = raw_names[0].lstrip("/")
            elif isinstance(raw_names, str):
                name = raw_names.strip().lstrip("/")
            else:
                name = c.get("ID", "unnamed")[:12]

            if name in exclude_list:
                continue
            if target_containers != "*" and name not in target_containers:
                continue

            state = str(c.get("State", "")).lower()
            status_desc = str(c.get("Status", "")).lower()

            # Check if container stopped cleanly with exit code 0
            is_clean_exit = False
            if "exited" in state or "exited" in status_desc:
                if re.search(r"\bexited\s*\(\s*0\s*\)", status_desc) or c.get("ExitCode") == 0:
                    is_clean_exit = True

            if is_clean_exit and ignore_exited_0:
                healthy_count += 1
                continue

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
                details={"issues": issues, "healthy_count": healthy_count, "total": len(containers)},
            )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"All {healthy_count} Docker containers healthy",
            details={"healthy_count": healthy_count, "total": len(containers)},
        )
