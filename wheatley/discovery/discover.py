"""Wheatley Auto-Discovery Engine

Inspects Proxmox nodes, resolves guest IP addresses, probes active services against
fingerprints in services.yaml, and generates recommended configuration drafts.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import socket
from typing import Any, Dict, List, Optional
import urllib3
import httpx
import yaml
from pydantic import BaseModel, Field

from wheatley.config import WheatleyConfig
from wheatley.tools.proxmox import ProxmoxClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("wheatley.discovery")


class DiscoveredService(BaseModel):
    service_id: str
    name: str
    category: str
    port: int
    protocol: str = "http"
    path: str = "/"
    tls_verify: bool = True
    url_template: str
    ip: str


class DiscoveredGuest(BaseModel):
    node: str
    vmid: int
    name: str
    type: str  # qemu or lxc
    status: str  # running or stopped
    onboot: bool = False
    ips: List[str] = Field(default_factory=list)
    services: List[DiscoveredService] = Field(default_factory=list)
    has_docker: bool = False
    is_self: bool = False

    @property
    def guest_spec(self) -> str:
        return f"{self.node}/{self.vmid}"


class InventoryDiscovery:
    """Discovers guests and services across configured Proxmox nodes."""

    def __init__(self, config: WheatleyConfig, fingerprints_path: Optional[str | Path] = None):
        self.config = config
        if fingerprints_path:
            self.fingerprints_file = Path(fingerprints_path)
        else:
            self.fingerprints_file = Path(__file__).parent / "services.yaml"
        self.services_catalog = self._load_fingerprints()

    def _load_fingerprints(self) -> List[Dict[str, Any]]:
        if not self.fingerprints_file.is_file():
            logger.warning("Services fingerprints file not found at %s", self.fingerprints_file)
            return []
        try:
            with open(self.fingerprints_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data.get("services", [])
        except Exception as e:
            logger.error("Failed to load service fingerprints: %s", e)
            return []

    async def probe_tcp_port(self, ip: str, port: int, timeout: float = 1.0) -> bool:
        """Fast non-blocking TCP socket check."""
        try:
            conn = asyncio.open_connection(ip, port)
            reader, writer = await asyncio.wait_for(conn, timeout=timeout)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def probe_http_service(
        self, ip: str, service: Dict[str, Any], guest_spec: str = "guest", timeout: float = 2.0
    ) -> Optional[DiscoveredService]:
        """Probe an IP against an HTTP/HTTPS fingerprint definition."""
        port = service.get("port")
        proto = service.get("protocol", "http")
        path = service.get("path", "/")
        tls_verify = service.get("tls_verify", True)
        expected_status = service.get("expected_status", [200])
        match_body = service.get("match_body")

        # Fast TCP pre-flight
        if not await self.probe_tcp_port(ip, port, timeout=1.0):
            return None

        url = f"{proto}://{ip}:{port}{path}"
        try:
            async with httpx.AsyncClient(verify=tls_verify, timeout=timeout) as client:
                res = await client.get(url)
                if expected_status and res.status_code not in expected_status:
                    return None
                if match_body and match_body not in res.text:
                    return None

                port_str = f":{port}" if not ((proto == "http" and port == 80) or (proto == "https" and port == 443)) else ""
                url_template = f"{proto}://{{guest:{guest_spec}}}{port_str}{path}"

                return DiscoveredService(
                    service_id=service["id"],
                    name=service.get("name", service["id"]),
                    category=service.get("category", "general"),
                    port=port,
                    protocol=proto,
                    path=path,
                    tls_verify=tls_verify,
                    url_template=url_template,
                    ip=ip,
                )
        except Exception:
            return None

    async def scan_guest(self, client: ProxmoxClient, guest_info: Dict[str, Any]) -> DiscoveredGuest:
        """Scan a single VM/CT: fetch config, resolve IPs, and probe services."""
        vmid = int(guest_info.get("vmid"))
        gtype = guest_info.get("type", "qemu")
        name = guest_info.get("name", f"guest-{vmid}")
        status = guest_info.get("status", "stopped")

        loop = asyncio.get_running_loop()
        guest_cfg = await loop.run_in_executor(None, client.get_guest_config, vmid, gtype)
        ips = await loop.run_in_executor(None, client.get_guest_ips, vmid, gtype)

        onboot = bool(guest_cfg.get("onboot", False) or guest_info.get("onboot", False))
        is_self = False
        if self.config.self_guest and self.config.self_guest == f"{client.name}/{vmid}":
            is_self = True
        elif name.lower() in ("wheatley", "sentinel"):
            is_self = True

        has_docker = "docker" in name.lower() or "docker" in guest_cfg.get("description", "").lower()

        discovered = DiscoveredGuest(
            node=client.name,
            vmid=vmid,
            name=name,
            type=gtype,
            status=status,
            onboot=onboot,
            ips=ips,
            has_docker=has_docker,
            is_self=is_self,
        )

        # Probe services only if guest is running and has resolved IPs
        if status == "running" and ips and not is_self:
            target_ip = ips[0]  # Primary LAN IP
            guest_spec = f"{client.name}/{vmid}"
            tasks = []
            for svc in self.services_catalog:
                tasks.append(self.probe_http_service(target_ip, svc, guest_spec=guest_spec))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, DiscoveredService):
                    discovered.services.append(res)

        return discovered

    async def scan_all(self) -> List[DiscoveredGuest]:
        """Scan all nodes and guests in parallel."""
        all_guests: List[DiscoveredGuest] = []
        loop = asyncio.get_running_loop()

        for node_cfg in self.config.nodes:
            # Skip nodes without valid tokens
            if "xxxx" in (node_cfg.tokens.monitor or ""):
                logger.warning("Skipping unconfigured node: %s", node_cfg.name)
                continue

            try:
                client = ProxmoxClient(node_cfg)
                raw_guests = await loop.run_in_executor(None, client.get_guests)
                tasks = [self.scan_guest(client, g) for g in raw_guests]
                node_guests = await asyncio.gather(*tasks)
                all_guests.extend(node_guests)
            except Exception as e:
                logger.error("Failed scanning node %s: %s", node_cfg.name, e)

        return all_guests

    def generate_draft_config(self, guests: List[DiscoveredGuest]) -> Dict[str, Any]:
        """Generate a complete recommended config dictionary based on discovery."""
        # Find self guest
        self_guest_spec = None
        for g in guests:
            if g.is_self:
                self_guest_spec = g.guest_spec
                break

        # Find stopped onboot guests to suggest for exclusion
        stopped_clones = [
            g.guest_spec for g in guests if g.status == "stopped" and any(kw in g.name.lower() for kw in ("ha", "backup", "clone"))
        ]

        checks: List[Dict[str, Any]] = [
            {
                "id": "node-health",
                "type": "node_metrics",
                "enabled": True,
                "interval_seconds": 60,
                "params": {
                    "nodes": "*",
                    "cpu_crit_percent": 90,
                    "mem_crit_percent": 90,
                    "duration_minutes": 5,
                },
            },
            {
                "id": "storage-health",
                "type": "storage",
                "enabled": True,
                "interval_seconds": 120,
                "params": {"nodes": "*", "warn_percent": 85, "crit_percent": 95},
            },
            {
                "id": "guests-up",
                "type": "guest_state",
                "enabled": True,
                "interval_seconds": 60,
                "params": {
                    "select": {"onboot": True},
                    "exclude": stopped_clones,
                },
            },
        ]

        # Add checks for each discovered service
        for g in guests:
            for svc in g.services:
                check_dict: Dict[str, Any] = {
                    "id": f"{svc.service_id}-{g.vmid}",
                    "type": "http",
                    "enabled": True,
                    "interval_seconds": 60,
                    "params": {"url": svc.url_template},
                }
                if not svc.tls_verify:
                    check_dict["params"]["tls_verify"] = False
                checks.append(check_dict)

            # If guest has Docker
            if g.has_docker:
                checks.append(
                    {
                        "id": f"docker-{g.vmid}",
                        "type": "docker",
                        "enabled": True,
                        "interval_seconds": 60,
                        "params": {
                            "guest": g.guest_spec,
                            "containers": "*",
                            "alert_on": ["exited", "unhealthy", "restarting"],
                        },
                    }
                )

        # Re-use existing nodes, budget, chat, allowlist from active config
        draft: Dict[str, Any] = {
            "nodes": [n.model_dump() for n in self.config.nodes],
            "self_guest": self_guest_spec or self.config.self_guest,
            "budget": self.config.budget.model_dump(),
            "chat": self.config.chat.model_dump(exclude_none=True),
            "checks": checks,
            "allowlist": self.config.allowlist,
        }
        return draft

    def format_summary_markdown(self, guests: List[DiscoveredGuest]) -> str:
        """Format discovery results as rich markdown."""
        lines = ["🔍 *Wheatley Auto-Discovery Summary*\n"]

        # Group by node
        nodes: Dict[str, List[DiscoveredGuest]] = {}
        for g in guests:
            nodes.setdefault(g.node, []).append(g)

        total_services = 0
        for node_name, node_guests in sorted(nodes.items()):
            lines.append(f"🖥️ *Node: `{node_name}`* ({len(node_guests)} guests)")
            for g in sorted(node_guests, key=lambda x: x.vmid):
                status_icon = "🟢" if g.status == "running" else "⚪"
                gtype = "CT" if g.type == "lxc" else "VM"
                ip_str = f" `{g.ips[0]}`" if g.ips else " _(no IP)_"
                self_tag = " 🛡️ _(self)_" if g.is_self else ""
                lines.append(f"  {status_icon} *{gtype} {g.vmid} ({g.name})*{self_tag} —{ip_str}")

                if g.services:
                    for s in g.services:
                        total_services += 1
                        lines.append(f"     └─ 🎯 *{s.name}* (Port `{s.port}`) -> `{s.url_template}`")
                if g.has_docker:
                    lines.append(f"     └─ 🐳 *Docker Environment Detected*")

            lines.append("")

        lines.extend([
            "📋 *Recommended Check Additions:*",
            f"• *Discovered Services:* `{total_services}`",
            f"• *Docker Hosts:* `{len([g for g in guests if g.has_docker])}`",
            f"• *Excluded Inactive Clones:* `{len([g for g in guests if g.status == 'stopped' and any(kw in g.name.lower() for kw in ('ha', 'backup', 'clone'))])}`",
            "\nRun `wheatley discover --output /etc/wheatley/config.draft.yaml` to export.",
        ])
        return "\n".join(lines)
