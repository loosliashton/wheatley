"""Proxmox VE API Client Wrapper for Wheatley"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
import urllib3
from proxmoxer import ProxmoxAPI

from wheatley.config import NodeConfig

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("wheatley.tools.proxmox")


class ProxmoxClient:
    """Wrapper around proxmoxer for a single Proxmox node."""

    def __init__(self, node_config: NodeConfig):
        self.node_config = node_config
        self.name = node_config.name
        self._api: Optional[ProxmoxAPI] = None

    def _get_api(self) -> ProxmoxAPI:
        if self._api is None:
            token_str = self.node_config.tokens.monitor
            if not token_str or "=" not in token_str or "!" not in token_str:
                raise ValueError(
                    f"Invalid token format for node '{self.name}'. Expected user@realm!tokenid=secret"
                )
            user_part, secret = token_str.split("=", 1)
            user, token_name = user_part.split("!", 1)

            url = self.node_config.api_url.replace("https://", "").replace("http://", "")
            host_part = url.split("/")[0]
            if ":" in host_part:
                host, port_str = host_part.split(":")
                port = int(port_str)
            else:
                host = host_part
                port = 8006

            self._api = ProxmoxAPI(
                host,
                port=port,
                user=user,
                token_name=token_name,
                token_value=secret,
                verify_ssl=False,
                timeout=10,
            )
        return self._api

    def get_node_status(self) -> Dict[str, Any]:
        """Fetch node CPU, RAM, disk, loadavg, and uptime."""
        api = self._get_api()
        return api.nodes(self.name).status.get()

    def get_guests(self) -> List[Dict[str, Any]]:
        """Fetch list of all QEMU VMs and LXCs on this node."""
        api = self._get_api()
        guests = []
        try:
            qemu_list = api.nodes(self.name).qemu.get()
            for vm in qemu_list:
                vm["type"] = "qemu"
                guests.append(vm)
        except Exception as e:
            logger.warning("Error fetching QEMU list for node %s: %s", self.name, e)

        try:
            lxc_list = api.nodes(self.name).lxc.get()
            for ct in lxc_list:
                ct["type"] = "lxc"
                guests.append(ct)
        except Exception as e:
            logger.warning("Error fetching LXC list for node %s: %s", self.name, e)

        return guests

    def get_storage(self) -> List[Dict[str, Any]]:
        """Fetch list of storage pools and usage."""
        api = self._get_api()
        return api.nodes(self.name).storage.get()

    def get_guest_config(self, vmid: int, guest_type: str) -> Dict[str, Any]:
        """Fetch guest configuration (onboot, cores, memory, net0, etc.)."""
        api = self._get_api()
        try:
            if guest_type == "qemu":
                return api.nodes(self.name).qemu(vmid).config.get()
            elif guest_type == "lxc":
                return api.nodes(self.name).lxc(vmid).config.get()
        except Exception as e:
            logger.warning("Error fetching config for %s/%s on %s: %s", guest_type, vmid, self.name, e)
        return {}

    def get_guest_network_interfaces(self, vmid: int, guest_type: str) -> List[Dict[str, Any]]:
        """Fetch network interfaces / IP addresses for a guest."""
        api = self._get_api()
        if guest_type == "qemu":
            try:
                res = api.nodes(self.name).qemu(vmid).agent("network-get-interfaces").get()
                return res.get("result", [])
            except Exception:
                return []
        elif guest_type == "lxc":
            try:
                res = api.nodes(self.name).lxc(vmid).interfaces.get()
                return res if isinstance(res, list) else []
            except Exception:
                return []
        return []

    def get_guest_ips(self, vmid: int, guest_type: str) -> List[str]:
        """Resolve clean LAN IPv4 addresses for a guest."""
        import re
        ips: List[str] = []
        ifaces = self.get_guest_network_interfaces(vmid, guest_type)
        for iface in ifaces:
            for ip_info in iface.get("ip-addresses", []):
                addr = ip_info.get("ip-address") or ip_info.get("ip")
                if addr and ":" not in addr:
                    if not addr.startswith(("127.", "169.254.", "0.")):
                        ips.append(addr)
            inet = iface.get("inet", "")
            if inet and "/" in inet:
                addr = inet.split("/")[0]
                if addr and not addr.startswith(("127.", "169.254.", "0.")):
                    ips.append(addr)

        if not ips:
            cfg = self.get_guest_config(vmid, guest_type)
            for k, v in cfg.items():
                if k.startswith("net") and isinstance(v, str):
                    match = re.search(r"ip=([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", v)
                    if match:
                        addr = match.group(1)
                        if not addr.startswith(("127.", "169.254.", "0.")):
                            ips.append(addr)

        seen = set()
        res = []
        for ip in ips:
            if ip not in seen:
                seen.add(ip)
                res.append(ip)
        return res
