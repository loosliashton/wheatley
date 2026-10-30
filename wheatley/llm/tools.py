"""Wheatley LLM Agent Toolbox - Tool Definitions and Execution Handlers"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from wheatley.approvals import ApprovalStore
from wheatley.audit import AuditLogger
from wheatley.checks.engine import CheckEngine
from wheatley.config import WheatleyConfig
from wheatley.llm.base import ToolDefinition
from wheatley.tools.allowlist import DEFAULT_ALLOWLIST, is_command_allowed, validate_proposed_pattern
from wheatley.tools.proxmox import ProxmoxClient
from wheatley.tools.shell import ShellExecutor

logger = logging.getLogger("wheatley.llm.tools")


class CommandExecutionHalted(Exception):
    """Raised when an unallowed command halts the reasoning loop awaiting user approval."""

    def __init__(self, proposal: Any, halt_message: str):
        self.proposal = proposal
        self.halt_message = halt_message
        super().__init__(halt_message)


def build_tool_definitions() -> List[ToolDefinition]:
    """Define schemas for all functions available to the reasoning agent."""
    return [
        ToolDefinition(
            name="get_cluster_status",
            description="Query live CPU, RAM, uptime, and full guest inventory (VMs and LXC containers with VMID, type, name, and power status) across all Proxmox nodes.",
            parameters={"type": "object", "properties": {}},
        ),
        ToolDefinition(
            name="get_guest_details",
            description="Get detailed status, IP addresses, specs, and config for a VM or LXC container.",
            parameters={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Node name (e.g. pve-node-01, pve-node-02)"},
                    "vmid": {"type": "integer", "description": "Virtual Machine or Container ID"},
                },
                "required": ["node", "vmid"],
            },
        ),
        ToolDefinition(
            name="get_check_states",
            description="List all monitored health checks, their current state (OK, CRIT, WARN, MUTED), and last results.",
            parameters={"type": "object", "properties": {}},
        ),
        ToolDefinition(
            name="run_health_check",
            description="Execute a specific configured health check on-demand and return its live result and metrics.",
            parameters={
                "type": "object",
                "properties": {
                    "check_id": {"type": "string", "description": "Check ID (e.g. web-app, docker-containers, node-health)"},
                },
                "required": ["check_id"],
            },
        ),
        ToolDefinition(
            name="get_recent_audit_logs",
            description="Retrieve recent security, authorization, and operational audit events.",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max logs to return (default 10)"},
                },
            },
        ),
        ToolDefinition(
            name="run_host_command",
            description=(
                "Execute a read-only diagnostics command directly on a Proxmox host node (e.g. 'pve-node-01'). "
                "Permitted commands include: uptime, free -m, df -h, journalctl -n 20, systemctl status <unit>, "
                "pvesm status, smartctl -H <device>, sensors -j, zpool status, etc. "
                "Any non-allowlisted or mutating command is blocked and automatically converted to an action proposal."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Target Proxmox node name (e.g. pve-node-01, pve-node-02)"},
                    "command": {"type": "string", "description": "Shell command to execute on the host"},
                    "summary": {
                        "type": "string",
                        "description": "Clear plain-English summary of what the command does (e.g. 'Check ZFS storage pool health and errors')",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Explanation of why this specific command is needed to answer the user's inquiry or diagnose the issue",
                    },
                },
                "required": ["node", "command", "summary", "justification"],
            },
        ),
        ToolDefinition(
            name="run_guest_command",
            description=(
                "Execute a read-only diagnostics command inside a Proxmox LXC container or QEMU VM. "
                "Permitted commands include: uptime, free, df, docker ps, docker logs <container>, systemctl status <unit>, etc. "
                "Any non-allowlisted or mutating command is blocked and automatically converted to an action proposal."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Host node where the guest resides (e.g. pve-node-01)"},
                    "vmid": {"type": "integer", "description": "Virtual Machine or Container ID (e.g. 100, 102)"},
                    "command": {"type": "string", "description": "Shell command to execute inside the guest"},
                    "summary": {
                        "type": "string",
                        "description": "Clear plain-English summary of what the command does (e.g. 'Check active connections in database container')",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Explanation of why this specific command is needed to answer the user's inquiry or diagnose the issue",
                    },
                },
                "required": ["node", "vmid", "command", "summary", "justification"],
            },
        ),
        ToolDefinition(
            name="propose_action",
            description="Submit an action proposal for user approval before executing any mutating command (start, stop, restart, config change).",
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Target host or guest (e.g. pve-node-01/108)"},
                    "command": {"type": "string", "description": "Exact shell command to execute"},
                    "summary": {"type": "string", "description": "Plain English summary of what will happen"},
                    "risk": {"type": "string", "description": "Risk level: LOW, MEDIUM, or HIGH"},
                },
                "required": ["target", "command", "summary", "risk"],
            },
        ),
        ToolDefinition(
            name="propose_check_config_update",
            description=(
                "Propose an update to the checks configuration file (checks.yaml) to add/remove container exclusions, "
                "adjust check thresholds, or modify check parameters. "
                "This creates an interactive proposal card for the user to review and approve with a diff before anything is changed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "check_id": {"type": "string", "description": "The ID of the check to update (e.g. 'docker-containers')"},
                    "action": {
                        "type": "string",
                        "enum": ["add_exclude", "remove_exclude", "set_param"],
                        "description": "The type of configuration modification",
                    },
                    "key": {"type": "string", "description": "Parameter key to update (e.g. 'exclude' or parameter name)"},
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of string values (e.g. container names to exclude)",
                    },
                    "value": {"type": "string", "description": "Scalar value if setting a single parameter"},
                    "summary": {"type": "string", "description": "Clear explanation of the proposed change"},
                },
                "required": ["check_id", "action", "summary"],
            },
        ),
    ]


class AgentToolbox:
    """Encapsulates Proxmox, CheckEngine, ShellExecutor, and ApprovalStore tool execution for the LLM agent."""

    def __init__(
        self,
        config: WheatleyConfig,
        audit: AuditLogger,
        approvals: ApprovalStore,
        shell: ShellExecutor,
        check_engine: Optional[CheckEngine] = None,
        proxmox_clients: Optional[Dict[str, ProxmoxClient]] = None,
        on_proposal: Optional[Callable[[Any], None]] = None,
    ):
        self.config = config
        self.audit = audit
        self.approvals = approvals
        self.shell = shell
        self.check_engine = check_engine
        self.clients = proxmox_clients or {}
        self.on_proposal = on_proposal
        self.tool_definitions: List[ToolDefinition] = build_tool_definitions()

    def _emit_proposal(self, prop: Any) -> None:
        if self.on_proposal:
            self.on_proposal(prop)

    def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Dispatch a function call requested by the model to the appropriate local handler."""
        logger.info("Executing tool '%s' with args %s", name, args)
        try:
            if name == "get_cluster_status":
                return self.get_cluster_status()
            elif name == "get_guest_details":
                return self.get_guest_details(args.get("node", ""), int(args.get("vmid", 0)))
            elif name == "get_check_states":
                return self.get_check_states()
            elif name == "run_health_check":
                return self.run_health_check(args.get("check_id", ""))
            elif name == "run_host_command":
                return self.run_host_command(
                    node=args.get("node", ""),
                    command=args.get("command", ""),
                    summary=args.get("summary"),
                    justification=args.get("justification"),
                )
            elif name == "run_guest_command":
                return self.run_guest_command(
                    node=args.get("node", ""),
                    vmid=int(args.get("vmid", 0)),
                    command=args.get("command", ""),
                    summary=args.get("summary"),
                    justification=args.get("justification"),
                )
            elif name == "get_recent_audit_logs":
                return self.get_audit_logs(int(args.get("limit", 10)))
            elif name == "propose_action":
                return self.propose_action(
                    target=args.get("target", "unknown"),
                    command=args.get("command", ""),
                    summary=args.get("summary", ""),
                    risk=args.get("risk", "MEDIUM"),
                )
            elif name == "propose_check_config_update":
                return self.propose_check_config_update(
                    check_id=args.get("check_id", ""),
                    action=args.get("action", "add_exclude"),
                    key=args.get("key"),
                    values=args.get("values"),
                    value=args.get("value"),
                    summary=args.get("summary", ""),
                )
            else:
                return {"error": f"Unknown tool: '{name}'"}
        except CommandExecutionHalted:
            raise
        except Exception as e:
            logger.exception("Error executing tool '%s': %s", name, e)
            return {"error": str(e)}

    async def execute_async(self, name: str, args: Dict[str, Any]) -> Any:
        """Async tool dispatcher that offloads synchronous Proxmox HTTP calls to a worker thread."""
        if name == "get_cluster_status":
            logger.info("Executing tool 'get_cluster_status' asynchronously")
            return await asyncio.to_thread(self.get_cluster_status)
        if name == "get_guest_details":
            logger.info("Executing tool 'get_guest_details' asynchronously with args %s", args)
            return await asyncio.to_thread(
                self.get_guest_details,
                args.get("node", ""),
                int(args.get("vmid", 0)),
            )

        res = self.execute(name, args)
        if asyncio.iscoroutine(res):
            return await res
        return res

    def get_cluster_status(self) -> Dict[str, Any]:
        status_report = {}
        for name, client in self.clients.items():
            try:
                node_st = client.get_node_status()
                guests = client.get_guests()
                status_report[name] = {
                    "cpu_percent": round(node_st.get("cpu", 0) * 100, 1),
                    "memory_used_gb": round(node_st.get("memory", {}).get("used", 0) / (1024**3), 1),
                    "memory_total_gb": round(node_st.get("memory", {}).get("total", 0) / (1024**3), 1),
                    "uptime_seconds": node_st.get("uptime", 0),
                    "running_guests": [g.get("name") for g in guests if g.get("status") == "running"],
                    "stopped_guests": [g.get("name") for g in guests if g.get("status") == "stopped"],
                    "guests": [
                        {
                            "vmid": g.get("vmid"),
                            "name": g.get("name"),
                            "type": g.get("type", "qemu"),
                            "status": g.get("status"),
                        }
                        for g in guests
                    ],
                }
            except Exception as e:
                status_report[name] = {"error": str(e)}
        return {"nodes": status_report}

    def get_guest_details(self, node: str, vmid: int) -> Dict[str, Any]:
        client = self.clients.get(node)
        if not client:
            return {"error": f"Node '{node}' not found"}

        for g in client.get_guests():
            if g.get("vmid") == vmid:
                gtype = g.get("type", "qemu")
                cfg = client.get_guest_config(vmid, gtype)
                ips = client.get_guest_ips(vmid, gtype)
                return {
                    "vmid": vmid,
                    "name": g.get("name"),
                    "type": gtype,
                    "status": g.get("status"),
                    "ips": ips,
                    "onboot": cfg.get("onboot", 0),
                    "memory_mb": cfg.get("memory"),
                    "cores": cfg.get("cores"),
                }
        return {"error": f"Guest {node}/{vmid} not found"}

    def get_check_states(self) -> Dict[str, Any]:
        if not self.check_engine:
            return {"error": "Check engine not active"}
        states = self.check_engine.get_check_states()
        summary = []
        for s in states:
            summary.append({
                "id": s["id"],
                "type": s["type"],
                "status": s["status"],
                "is_muted": s["is_muted"],
                "consecutive_failures": s["consecutive_failures"],
                "summary": s["last_result"].summary if s.get("last_result") else None,
            })
        return {"checks": summary}

    async def run_health_check(self, check_id: str) -> Dict[str, Any]:
        if not self.check_engine:
            return {"error": "Check engine not active"}
        if check_id not in self.check_engine.checks:
            return {"error": f"Check '{check_id}' not found"}

        res = await self.check_engine.execute_check(check_id)
        return {
            "check_id": res.check_id,
            "status": res.status.value,
            "summary": res.summary,
            "metrics": res.metrics,
            "details": res.details,
        }

    def _build_candidate_pattern(self, command: str) -> str:
        cmd_parts = command.strip().split()
        if len(cmd_parts) >= 2:
            candidate = f"{cmd_parts[0]} {cmd_parts[1]} *"
            valid, _ = validate_proposed_pattern(candidate)
            if valid:
                return candidate
        return command.strip()

    async def run_host_command(
        self,
        node: str,
        command: str,
        summary: Optional[str] = None,
        justification: Optional[str] = None,
    ) -> Dict[str, Any]:
        target = node
        active_user_rules = self.approvals.get_active_allowlist(target)
        effective_allowlist = list(set(DEFAULT_ALLOWLIST + list(self.config.allowlist))) + active_user_rules
        allowed, reason = is_command_allowed(command, effective_allowlist)
        if not allowed:
            logger.info("Host command '%s' on %s blocked by allowlist (%s); halting for approval", command, node, reason)
            candidate_pattern = self._build_candidate_pattern(command)
            prop_summary = summary or f"Execute diagnostic command on {target}"
            prop = self.approvals.create_proposal(
                proposal_type="ALLOWLIST_RULE",
                target=target,
                command=command,
                pattern=candidate_pattern,
                summary=prop_summary,
                justification=justification,
                risk="LOW",
            )
            self._emit_proposal(prop)
            halt_msg = (
                f"I need permission to execute `{command}` on `{target}` to proceed with your request. "
                "Please choose whether to allow this command using the options below."
            )
            raise CommandExecutionHalted(proposal=prop, halt_message=halt_msg)

        res = await self.shell.execute_on_host(node, command, timeout=20)
        return {
            "status": "SUCCESS" if res.success else "FAILED",
            "node": node,
            "command": command,
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "timed_out": res.timed_out,
        }

    async def run_guest_command(
        self,
        node: str,
        vmid: int,
        command: str,
        summary: Optional[str] = None,
        justification: Optional[str] = None,
    ) -> Dict[str, Any]:
        target = f"{node}/{vmid}"
        active_user_rules = self.approvals.get_active_allowlist(target)
        effective_allowlist = list(set(DEFAULT_ALLOWLIST + list(self.config.allowlist))) + active_user_rules
        allowed, reason = is_command_allowed(command, effective_allowlist)
        if not allowed:
            logger.info("Guest command '%s' on %s blocked by allowlist (%s); halting for approval", command, target, reason)
            candidate_pattern = self._build_candidate_pattern(command)
            prop_summary = summary or f"Execute diagnostic command on {target}"
            prop = self.approvals.create_proposal(
                proposal_type="ALLOWLIST_RULE",
                target=target,
                command=command,
                pattern=candidate_pattern,
                summary=prop_summary,
                justification=justification,
                risk="LOW",
            )
            self._emit_proposal(prop)
            halt_msg = (
                f"I need permission to execute `{command}` on `{target}` to proceed with your request. "
                "Please choose whether to allow this command using the options below."
            )
            raise CommandExecutionHalted(proposal=prop, halt_message=halt_msg)

        res = await self.shell.execute_in_guest(node, vmid, command, timeout=20)
        return {
            "status": "SUCCESS" if res.success else "FAILED",
            "target": target,
            "command": command,
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "timed_out": res.timed_out,
        }

    def get_audit_logs(self, limit: int = 10) -> Dict[str, Any]:
        logs = self.audit.get_recent_logs(limit=min(limit, 20))
        return {"audit_events": logs}

    def propose_action(self, target: str, command: str, summary: str, risk: str) -> Dict[str, Any]:
        if self.config.self_guest and target == self.config.self_guest:
            return {
                "status": "REFUSED",
                "reason": f"Self-protection triggered: Target '{target}' is running the Wheatley agent.",
            }

        prop = self.approvals.create_proposal(
            proposal_type="ACTION",
            target=target,
            command=command,
            summary=summary,
            risk=risk,
        )
        self._emit_proposal(prop)

        return {
            "status": "PROPOSED",
            "proposal_id": prop.id,
            "proposal_type": prop.proposal_type,
            "target": target,
            "command": command,
            "summary": summary,
            "risk": risk,
            "note": f"Action proposal ({prop.id}) prepared for user review. Explain this to the user; state changes require explicit interactive button approval.",
        }

    def propose_check_config_update(
        self,
        check_id: str,
        action: str,
        summary: str,
        key: Optional[str] = None,
        values: Optional[List[str]] = None,
        value: Any = None,
    ) -> Dict[str, Any]:
        from wheatley.tools.config_editor import modify_check_config

        checks_file = getattr(self.config, "checks_file", None)
        if not checks_file:
            checks_file = "/etc/wheatley/checks.yaml"

        file_path = Path(checks_file)
        if not file_path.is_file():
            return {
                "status": "ERROR",
                "error": f"Checks configuration file '{file_path}' does not exist. Please configure checks in '{file_path}'.",
            }

        try:
            new_yaml_text, diff, _ = modify_check_config(
                file_path=file_path,
                check_id=check_id,
                action=action,
                key=key,
                values=values,
                value=value,
            )
        except Exception as e:
            logger.warning("Failed to construct check config update: %s", e)
            return {"status": "ERROR", "error": f"Invalid configuration update: {e}"}

        command_payload = json.dumps({
            "file": str(file_path),
            "check_id": check_id,
            "new_yaml": new_yaml_text,
            "diff": diff,
        })

        prop = self.approvals.create_proposal(
            proposal_type="CONFIG_UPDATE",
            target=str(file_path),
            command=command_payload,
            summary=summary,
            risk="LOW",
        )
        self._emit_proposal(prop)

        return {
            "status": "PROPOSED",
            "proposal_id": prop.id,
            "target": str(file_path),
            "check_id": check_id,
            "summary": summary,
            "diff": diff,
            "note": "Configuration update proposal prepared with diff. Explain this to the user; changes require explicit interactive button approval.",
        }

