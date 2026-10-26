"""Wheatley Shell Execution Engine

Provides secure, non-interactive execution of read-only and approved commands:
1. Host execution: via asyncssh to Proxmox VE nodes.
2. LXC execution: via 'pct exec <vmid> -- <cmd>' on the host.
3. VM execution: via 'qm guest exec <vmid> -- <cmd>' on the host with JSON parsing.
4. Auto-detection of guest types (LXC vs QEMU).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import shlex
from typing import Dict, Optional
import asyncssh

from wheatley.config import NodeConfig, WheatleyConfig

logger = logging.getLogger("wheatley.tools.shell")

# Commands on Proxmox VE host that require sudo when run as wheatley user
SUDO_HOST_COMMANDS = {
    "pct",
    "qm",
    "pvesm",
    "zpool",
    "smartctl",
    "sensors",
    "journalctl",
    "systemctl",
}


@dataclass
class ShellResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ShellExecutor:
    """Manages SSH connections to Proxmox VE nodes and dispatches commands."""

    def __init__(self, config: WheatleyConfig):
        self.config = config
        self.nodes: Dict[str, NodeConfig] = {n.name: n for n in config.nodes}
        self._conns: Dict[str, asyncssh.SSHClientConnection] = {}
        self._lock = asyncio.Lock()

    def get_node_config(self, node_name: str) -> Optional[NodeConfig]:
        return self.nodes.get(node_name)

    async def _get_connection(self, node_name: str) -> asyncssh.SSHClientConnection:
        """Get an active SSH connection to a node, reconnecting if disconnected."""
        node = self.nodes.get(node_name)
        if not node:
            raise ValueError(f"Unknown Proxmox node '{node_name}'. Configured nodes: {list(self.nodes.keys())}")

        async with self._lock:
            conn = self._conns.get(node_name)
            if conn is not None:
                # Check if connection is still active
                if not conn.is_closed():
                    return conn
                else:
                    self._conns.pop(node_name, None)

            ssh_cfg = node.ssh
            key_paths = []
            if ssh_cfg.key_path and Path(ssh_cfg.key_path).is_file():
                key_paths.append(ssh_cfg.key_path)
            else:
                default_key = Path("/var/lib/wheatley/.ssh/id_ed25519")
                if default_key.is_file():
                    key_paths.append(str(default_key))

            logger.info("Opening SSH connection to %s (%s:%d) as %s", node_name, ssh_cfg.host, ssh_cfg.port, ssh_cfg.user)
            conn = await asyncssh.connect(
                host=ssh_cfg.host,
                port=ssh_cfg.port,
                username=ssh_cfg.user,
                client_keys=key_paths if key_paths else None,
                known_hosts=None,  # CT 110 runs in automated cluster env
                login_timeout=10,
            )
            self._conns[node_name] = conn
            return conn

    async def execute_on_host(
        self, node: str, command: str, timeout: int = 30
    ) -> ShellResult:
        """Execute a command directly on a Proxmox VE host node."""
        cmd_to_run = command.strip()
        first_word = cmd_to_run.split()[0] if cmd_to_run else ""
        first_basename = Path(first_word).name

        # Ensure sudo prefix if needed for restricted tools
        if first_basename in SUDO_HOST_COMMANDS and not cmd_to_run.startswith("sudo"):
            cmd_to_run = f"sudo {cmd_to_run}"

        try:
            conn = await self._get_connection(node)
            res = await asyncio.wait_for(conn.run(cmd_to_run, check=False), timeout=timeout)
            return ShellResult(
                command=command,
                exit_code=res.exit_status or 0,
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                timed_out=False,
            )
        except asyncio.TimeoutError:
            logger.warning("Command '%s' timed out after %ds on node %s", command, timeout, node)
            return ShellResult(
                command=command,
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
                timed_out=True,
            )
        except Exception as e:
            logger.error("SSH execution error on node %s for '%s': %s", node, command, e)
            # Remove stale connection if broken
            async with self._lock:
                self._conns.pop(node, None)
            return ShellResult(
                command=command,
                exit_code=255,
                stdout="",
                stderr=f"SSH execution error: {e}",
            )

    async def execute_in_lxc(
        self, node: str, vmid: int, command: str, timeout: int = 30
    ) -> ShellResult:
        """Execute a command inside an LXC container using pct exec."""
        # Wrap in bash -c to preserve pipeline and argument semantics inside the container
        escaped_cmd = shlex.quote(command)
        pct_cmd = f"sudo /usr/sbin/pct exec {vmid} -- /bin/bash -c {escaped_cmd}"
        return await self.execute_on_host(node, pct_cmd, timeout=timeout)

    async def execute_in_vm(
        self, node: str, vmid: int, command: str, timeout: int = 30
    ) -> ShellResult:
        """Execute a command inside a QEMU VM using qm guest exec and parse guest-agent output."""
        escaped_cmd = shlex.quote(command)
        qm_cmd = f"sudo /usr/sbin/qm guest exec {vmid} -- /bin/bash -c {escaped_cmd}"
        host_res = await self.execute_on_host(node, qm_cmd, timeout=timeout)

        if not host_res.success and not host_res.stdout:
            return host_res

        # Parse JSON output returned by qm guest exec
        try:
            data = json.loads(host_res.stdout)
            exit_code = data.get("exitcode", 0)
            stdout = data.get("out-data", "")
            stderr = data.get("err-data", "")
            return ShellResult(
                command=command,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
            )
        except json.JSONDecodeError:
            # Fallback to returning raw output
            return host_res

    async def execute_in_guest(
        self,
        node: str,
        vmid: int,
        command: str,
        guest_type: Optional[str] = None,
        timeout: int = 30,
    ) -> ShellResult:
        """Execute inside a guest container or VM, automatically choosing pct vs qm."""
        # If guest_type not specified, try to infer or default to qm/pct
        if not guest_type:
            # Quick check: try qm guest exec first; if VM not running or unknown, try pct
            guest_type = "qemu"

        if guest_type == "lxc":
            return await self.execute_in_lxc(node, vmid, command, timeout=timeout)
        else:
            res = await self.execute_in_vm(node, vmid, command, timeout=timeout)
            # If qm failed with 'not a VM' or VM configuration error, try pct
            if res.exit_code != 0 and ("Configuration file" in res.stderr or "does not exist" in res.stderr):
                return await self.execute_in_lxc(node, vmid, command, timeout=timeout)
            return res

    async def execute_guest_spec(
        self, guest_spec: str, command: str, timeout: int = 30
    ) -> ShellResult:
        """Execute inside a guest specified as 'node/vmid' (e.g. 'cave/100')."""
        spec = guest_spec.strip()
        if spec.startswith("{guest:") and spec.endswith("}"):
            spec = spec[7:-1].strip()

        if "/" not in spec:
            return ShellResult(
                command=command,
                exit_code=1,
                stdout="",
                stderr=f"Invalid guest spec '{guest_spec}', expected 'node/vmid'",
            )

        node_name, vmid_str = spec.split("/", 1)
        try:
            vmid = int(vmid_str)
        except ValueError:
            return ShellResult(
                command=command,
                exit_code=1,
                stdout="",
                stderr=f"Invalid VMID in guest spec '{guest_spec}'",
            )

        return await self.execute_in_guest(node_name, vmid, command, timeout=timeout)

    async def close(self) -> None:
        """Close all cached SSH connections."""
        async with self._lock:
            for node_name, conn in list(self._conns.items()):
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns.clear()
