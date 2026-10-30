"""Wheatley LLM Agent - Multi-Turn Reasoning, Tool Calling, and Automated Alert Investigation"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from wheatley.approvals import ApprovalStore
from wheatley.audit import AuditLogger
from wheatley.checks.engine import CheckEngine
from wheatley.config import WheatleyConfig
from wheatley.llm.base import BaseLLMProvider, LLMMessage, ToolCall, ToolDefinition
from wheatley.llm.memory import ConversationStore
from wheatley.llm.providers import get_llm_provider
from wheatley.llm.usage import UsageTracker
from wheatley.tools.allowlist import DEFAULT_ALLOWLIST, is_command_allowed, validate_proposed_pattern
from wheatley.tools.proxmox import ProxmoxClient
from wheatley.tools.shell import ShellExecutor

logger = logging.getLogger("wheatley.llm.agent")


class CommandExecutionHalted(Exception):
    """Raised when an unallowed command halts the reasoning loop awaiting user approval."""

    def __init__(self, proposal: Any, halt_message: str):
        self.proposal = proposal
        self.halt_message = halt_message
        super().__init__(halt_message)


class LLMAgent:
    """Orchestrates multi-turn reasoning loops, Proxmox tool execution, and alert diagnostics."""

    def __init__(
        self,
        config: WheatleyConfig,
        audit: AuditLogger,
        provider: Optional[BaseLLMProvider] = None,
        usage: Optional[UsageTracker] = None,
        check_engine: Optional[CheckEngine] = None,
        proxmox_clients: Optional[Dict[str, ProxmoxClient]] = None,
        shell: Optional[ShellExecutor] = None,
        **kwargs: Any,  # Absorb legacy kwargs like budget=...
    ):
        self.config = config
        self.audit = audit
        self.check_engine = check_engine
        self.clients = proxmox_clients or {}
        self.shell = shell or (check_engine.shell if check_engine else ShellExecutor(self.config))

        # Conversation Memory (SQLite-backed)
        self.memory = kwargs.get("memory") or ConversationStore(getattr(audit, "db_path", "/var/lib/wheatley/wheatley.db"))

        # Approval & Dynamic Allowlist Store (SQLite-backed)
        self.approvals = kwargs.get("approvals") or ApprovalStore(getattr(audit, "db_path", "/var/lib/wheatley/wheatley.db"))
        self.proposal_sink: Optional[Callable[[Any], Coroutine[Any, Any, None]]] = kwargs.get("proposal_sink")
        self.pending_proposals: List[Any] = []
        self._defer_proposals: bool = False

        # Usage tracker (records tokens without billing blocks/caps)
        if usage is not None:
            self.usage = usage
        elif "budget" in kwargs and hasattr(kwargs["budget"], "record_usage"):
            self.usage = kwargs["budget"]  # type: ignore
        else:
            self.usage = UsageTracker(self.audit)

        # LLM Provider
        if provider is not None:
            self.provider = provider
        else:
            self.provider = get_llm_provider(self.config.llm)

        self._tools = self._register_tools()

    @property
    def is_configured(self) -> bool:
        return self.provider.is_configured

    @property
    def model_name(self) -> str:
        return self.provider.model_name

    @property
    def provider_name(self) -> str:
        return self.provider.provider_name

    def _register_tools(self) -> List[ToolDefinition]:
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

    def build_system_prompt(self) -> str:
        """Constructs system instruction dynamically from live homelab configuration and inventory."""
        nodes_info = []
        for n in self.config.nodes:
            nodes_info.append(f"• Node: {n.name} (API: {n.api_url})")

        checks_info = []
        if self.check_engine:
            states = self.check_engine.get_check_states()
            for s in states:
                status_str = f"MUTED until {s['muted_until'].strftime('%H:%M UTC')}" if s["is_muted"] else s["status"]
                checks_info.append(f"• {s['id']} ({s['type']}): {status_str}")
        else:
            for c in self.config.checks:
                if c.enabled:
                    checks_info.append(f"• {c.id} ({c.type})")

        lines = [
            "You are Wheatley, an autonomous homelab monitoring and administration assistant for Proxmox VE.",
            "You help the homelab administrator monitor cluster telemetry, diagnose failures, and manage workloads.",
            "",
            "## Current Cluster Inventory",
            "Configured Proxmox Nodes:",
            "\n".join(nodes_info) if nodes_info else "No nodes configured.",
            "",
            "Active Health Checks:",
            "\n".join(checks_info) if checks_info else "No health checks active.",
            "",
            f"Agent Self-Protection: Guest '{self.config.self_guest or 'disabled'}' is running Wheatley. Never perform disruptive actions on this guest.",
            "",
            "## Core Operational Rules & Constraints",
            "1. Read-Only Diagnostics: Run diagnostic queries immediately using available tools. Always check live telemetry or recent logs before forming conclusions.",
            "2. Interactive Approval Gate for State Changes: You CANNOT mutate system state directly. If the user asks you to start, stop, restart, destroy, or modify any guest or configuration, you must first diagnose, explain the proposed command, and call the 'propose_action' function so the user can review and approve it via Telegram buttons.",
            "3. Telegram Mobile Formatting: Homelab administrators read your messages on Telegram mobile screens. Format your answers cleanly: use bullet points (•) for lists, **bold** for key names and metrics, and triple-backtick code blocks for CLI commands. Avoid markdown tables (they do not render well on mobile; format tabular data as bullet lists instead).",
            "4. Grounded in Facts: Base all diagnostic answers on tool output. If a service is down, report the exact status code, latency, or error message.",
            "5. Host & Guest Diagnostic Execution: You have access to 'run_host_command' and 'run_guest_command' to execute read-only diagnostic commands directly (e.g. lsblk, smartctl, sensors, uptime, free, df, docker ps, docker logs, systemctl status, journalctl). When calling these tools, you MUST provide a meaningful 'summary' explaining what the command does in plain English (do not just repeat the command) and a 'justification' explaining why this specific command is required. Non-allowlisted commands will automatically produce an interactive approval proposal for the user showing your summary and justification.",
            "6. Tool Budget & Anti-Fishing Efficiency: You have a strict limit of function calling iterations per query. Plan your diagnostics efficiently:",
            "   • NEVER make speculative, trial-and-error, or guessing calls (e.g. testing drive paths /dev/sda, /dev/sdb... or guessing service names).",
            "   • ALWAYS run discovery commands first: use 'lsblk' or 'smartctl --scan' to discover disks, or 'systemctl list-units' / 'systemctl list-timers' to find active units.",
            "   • If a diagnostic command requires user approval or is blocked, explain clearly to the user what command is required rather than repeatedly guessing alternatives.",
            "7. Structured Tool Calling vs Text: You must ALWAYS execute diagnostics or request mutations by invoking the tools ('run_guest_command', 'run_host_command', 'propose_action'). NEVER write out conversational text requesting permission (e.g. do NOT say 'I need permission to execute...'). The system intercepts non-allowlisted commands and automatically presents the interactive approval buttons to the user when you call the tool. Writing permission requests in text bypasses the proposal system and prevents commands from being executed.",
        ]
        return "\n".join(lines)

    def _execute_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """Dispatch a function call requested by the model to the appropriate local handler."""
        logger.info("Executing tool '%s' with args %s", name, args)
        try:
            if name == "get_cluster_status":
                return self._tool_get_cluster_status()
            elif name == "get_guest_details":
                return self._tool_get_guest_details(args.get("node", ""), int(args.get("vmid", 0)))
            elif name == "get_check_states":
                return self._tool_get_check_states()
            elif name == "run_health_check":
                return self._tool_run_health_check(args.get("check_id", ""))
            elif name == "run_host_command":
                return self._tool_run_host_command(
                    node=args.get("node", ""),
                    command=args.get("command", ""),
                    summary=args.get("summary"),
                    justification=args.get("justification"),
                )
            elif name == "run_guest_command":
                return self._tool_run_guest_command(
                    node=args.get("node", ""),
                    vmid=int(args.get("vmid", 0)),
                    command=args.get("command", ""),
                    summary=args.get("summary"),
                    justification=args.get("justification"),
                )
            elif name == "get_recent_audit_logs":
                return self._tool_get_audit_logs(int(args.get("limit", 10)))
            elif name == "propose_action":
                return self._tool_propose_action(
                    target=args.get("target", "unknown"),
                    command=args.get("command", ""),
                    summary=args.get("summary", ""),
                    risk=args.get("risk", "MEDIUM"),
                )
            elif name == "propose_check_config_update":
                return self._tool_propose_check_config_update(
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

    def _tool_get_cluster_status(self) -> Dict[str, Any]:
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

    def _tool_get_guest_details(self, node: str, vmid: int) -> Dict[str, Any]:
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

    def _tool_get_check_states(self) -> Dict[str, Any]:
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

    async def _tool_run_health_check(self, check_id: str) -> Dict[str, Any]:
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

    def _dispatch_proposal(self, prop: Any) -> None:
        if self._defer_proposals:
            self.pending_proposals.append(prop)
            return

        if self.proposal_sink:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.proposal_sink(prop))
            except RuntimeError:
                pass
            except Exception as e:
                logger.warning("Failed to dispatch proposal to sink: %s", e)

    def drain_pending_proposals(self) -> List[Any]:
        """Drain and return any proposals deferred during reasoning turns."""
        props = list(self.pending_proposals)
        self.pending_proposals.clear()
        return props

    async def _tool_run_host_command(
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
            cmd_parts = command.strip().split()
            candidate_pattern = None
            if len(cmd_parts) >= 2:
                candidate = f"{cmd_parts[0]} {cmd_parts[1]} *"
                valid, _ = validate_proposed_pattern(candidate)
                if valid:
                    candidate_pattern = candidate
            if not candidate_pattern:
                valid, _ = validate_proposed_pattern(command.strip())
                candidate_pattern = command.strip() if valid else command.strip()

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
            self._dispatch_proposal(prop)
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

    async def _tool_run_guest_command(
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
            cmd_parts = command.strip().split()
            candidate_pattern = None
            if len(cmd_parts) >= 2:
                candidate = f"{cmd_parts[0]} {cmd_parts[1]} *"
                valid, _ = validate_proposed_pattern(candidate)
                if valid:
                    candidate_pattern = candidate
            if not candidate_pattern:
                valid, _ = validate_proposed_pattern(command.strip())
                candidate_pattern = command.strip() if valid else command.strip()

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
            self._dispatch_proposal(prop)
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

    def _tool_get_audit_logs(self, limit: int = 10) -> Dict[str, Any]:
        logs = self.audit.get_recent_logs(limit=min(limit, 20))
        return {"audit_events": logs}

    def _tool_propose_action(self, target: str, command: str, summary: str, risk: str) -> Dict[str, Any]:
        # Refuse actions targeting self
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
        self._dispatch_proposal(prop)

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

    def _tool_propose_check_config_update(
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
        self._dispatch_proposal(prop)

        return {
            "status": "PROPOSED",
            "proposal_id": prop.id,
            "target": str(file_path),
            "check_id": check_id,
            "summary": summary,
            "diff": diff,
            "note": "Configuration update proposal prepared with diff. Explain this to the user; changes require explicit interactive button approval.",
        }

    def _truncate_tool_result(self, result: Any) -> Any:
        """Cap tool output payload to prevent context window overflow."""
        max_chars = getattr(self.config.llm, "max_tool_result_chars", 4000)
        if isinstance(result, str):
            if len(result) > max_chars:
                return result[:max_chars] + f"\n... [Output truncated at {max_chars} chars; refine query if needed]"
            return result

        try:
            raw_json = json.dumps(result, default=str)
            if len(raw_json) > max_chars:
                if isinstance(result, dict):
                    truncated_dict: Dict[str, Any] = {}
                    for k, v in result.items():
                        if isinstance(v, str) and len(v) > max_chars // 2:
                            truncated_dict[k] = v[: max_chars // 2] + "... [truncated]"
                        elif isinstance(v, list) and len(v) > 20:
                            truncated_dict[k] = v[:20]
                            truncated_dict[f"{k}_note"] = f"Truncated to 20 items (total was {len(v)})"
                        else:
                            truncated_dict[k] = v
                    new_raw = json.dumps(truncated_dict, default=str)
                    if len(new_raw) > max_chars:
                        return {
                            "truncated_data": new_raw[:max_chars],
                            "_warning": f"Tool result exceeded {max_chars} chars and was truncated.",
                        }
                    return truncated_dict
                return {
                    "truncated_data": raw_json[:max_chars],
                    "_warning": f"Tool result exceeded {max_chars} chars and was truncated.",
                }
        except Exception:
            pass
        return result

    def _format_llm_footer(self, tool_calls_count: int = 0) -> str:
        """Subtle italicized footer indicating the LLM model and tool executions."""
        if not getattr(self.config.llm, "show_footer", True):
            return ""
        model_name = self.model_name or self.provider_name or "ai"
        model_disp = model_name.replace("_", "-")
        if tool_calls_count > 0:
            plural = "s" if tool_calls_count != 1 else ""
            return f"\n\n_🧠 {model_disp} · {tool_calls_count} tool call{plural}_"
        return f"\n\n_🧠 {model_disp}_"

    def _intercept_pseudo_permission_call(self, text: Optional[str]) -> Optional[ToolCall]:
        """Detects if the LLM mimicked a canned permission prompt in text instead of invoking a tool."""
        if not text:
            return None
        m = re.search(
            r"(?:need|request|require)\s+permission\s+to\s+execute\s+[`\"](?P<cmd>.+?)[`\"]\s+on\s+(?:target\s+)?[`\"]?(?P<target>[a-zA-Z0-9_\-\.\/]+)[`\"]?",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return None
        cmd = m.group("cmd").strip()
        target = m.group("target").strip()
        if "/" in target:
            parts = target.split("/", 1)
            node = parts[0]
            try:
                vmid = int(parts[1])
                return ToolCall(
                    id="pseudo_intercepted",
                    name="run_guest_command",
                    arguments={
                        "node": node,
                        "vmid": vmid,
                        "command": cmd,
                        "summary": f"Execute diagnostic command on {target}",
                        "justification": "Required for requested diagnostic analysis",
                    },
                )
            except ValueError:
                pass
        return ToolCall(
            id="pseudo_intercepted",
            name="run_host_command",
            arguments={
                "node": target,
                "command": cmd,
                "summary": f"Execute diagnostic command on {target}",
                "justification": "Required for requested diagnostic analysis",
            },
        )

    def reset_conversation(self, chat_id: str) -> int:
        """Clear conversation history for a given chat session."""
        return self.memory.clear_history(chat_id)

    async def chat(
        self,
        prompt: str,
        chat_id: str = "default",
        context_note: Optional[str] = None,
    ) -> str:
        """Run the multi-turn conversational reasoning loop with SQLite memory persistence."""
        if not self.is_configured:
            return (
                f"⚠️ *LLM Provider '{self.provider_name}' is unconfigured.*\n\n"
                "To enable conversational reasoning and automated investigations:\n"
                "1. Update `llm:` in `/etc/wheatley/config.yaml` (provider: gemini, ollama, openai, groq, etc.)\n"
                "2. Add required API keys to `/etc/wheatley/secrets.env` (e.g. `GEMINI_API_KEY` or `OPENAI_API_KEY`)\n"
                "3. Restart Wheatley (`systemctl restart wheatley`)"
            )

        self.pending_proposals = []
        self._defer_proposals = True
        try:
            user_content = prompt
            if context_note:
                user_content = f"Context: {context_note}\n\nQuestion: {prompt}"

            # Clean up expired conversations in background
            try:
                self.memory.cleanup_old_conversations(
                    retention_days=getattr(self.config.llm, "history_retention_days", 30)
                )
            except Exception:
                pass

            # Load recent conversational history up to history_turns
            max_turns = getattr(self.config.llm, "history_turns", 10)
            session_timeout = getattr(self.config.llm, "session_timeout_minutes", 60)
            history: List[LLMMessage] = []
            if max_turns > 0:
                history = self.memory.get_history(
                    chat_id=chat_id,
                    max_turns=max_turns,
                    session_timeout_minutes=session_timeout,
                )

            # Build initial message chain
            current_user_msg = LLMMessage(role="user", content=user_content)
            messages: List[LLMMessage] = list(history) + [current_user_msg]

            # Record this user message to SQLite
            self.memory.add_message(chat_id=chat_id, role="user", content=user_content)

            system_instruction = self.build_system_prompt()
            total_input_tokens = 0
            total_output_tokens = 0
            total_tool_calls = 0
            iterations = 0
            max_iterations = getattr(self.config.llm, "max_tool_iterations", 10)

            while iterations < max_iterations:
                try:
                    response = await self.provider.generate_response(
                        messages=messages,
                        tools=self._tools,
                        system_instruction=system_instruction,
                    )
                except Exception as e:
                    logger.exception("LLM generation error: %s", e)
                    return f"❌ *LLM API Error ({self.provider_name}):* `{e}`"

                total_input_tokens += response.input_tokens
                total_output_tokens += response.output_tokens

                # Check if model hallucinated a text permission request instead of invoking a tool call
                if not response.tool_calls:
                    pseudo_tool = self._intercept_pseudo_permission_call(response.content)
                    if pseudo_tool:
                        logger.info(
                            "Intercepted text permission prompt; routing to tool '%s' with args %s",
                            pseudo_tool.name,
                            pseudo_tool.arguments,
                        )
                        response.tool_calls = [pseudo_tool]

                # If no tool calls requested, return the model's text response and persist it
                if not response.tool_calls:
                    self.usage.record_usage(
                        model=self.model_name,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        context=f"chat:{prompt[:30]}",
                    )
                    final_text = response.content or "I processed your request, but received an empty text response."
                    self.memory.add_message(chat_id=chat_id, role="assistant", content=final_text)
                    return f"{final_text}{self._format_llm_footer(total_tool_calls)}"

                total_tool_calls += len(response.tool_calls)

                # Append assistant message with requested tool calls
                messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content,
                        tool_calls=response.tool_calls,
                        raw=response.raw,
                    )
                )

                # Execute tool calls, truncate if excessive, and append results
                halted_exception: Optional[CommandExecutionHalted] = None
                for call in response.tool_calls:
                    try:
                        raw_result = self._execute_tool(call.name, call.arguments)
                        if asyncio.iscoroutine(raw_result):
                            raw_result = await raw_result
                    except CommandExecutionHalted as e:
                        halted_exception = e
                        break
                    except Exception as e:
                        logger.exception("Error executing tool '%s': %s", call.name, e)
                        raw_result = {"error": str(e)}

                    tool_result = self._truncate_tool_result(raw_result)
                    messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=call.id,
                            name=call.name,
                            content=tool_result,
                        )
                    )

                if halted_exception:
                    logger.info(
                        "Halting reasoning loop awaiting user approval for proposal %s on %s",
                        halted_exception.proposal.id,
                        halted_exception.proposal.target,
                    )
                    self.usage.record_usage(
                        model=self.model_name,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        context=f"chat:halted:{halted_exception.proposal.id}",
                    )
                    final_text = halted_exception.halt_message
                    self.memory.add_message(chat_id=chat_id, role="assistant", content=final_text)
                    return f"{final_text}{self._format_llm_footer(total_tool_calls)}"

                iterations += 1

            # Iteration cap reached - synthesize observations gathered so far into a cohesive answer
            try:
                logger.info("Reached maximum iterations (%d); synthesizing observations into final answer", max_iterations)
                synthesis_instruction = (
                    f"{system_instruction}\n\n"
                    "CRITICAL: You have reached the diagnostic tool iteration budget. "
                    "Synthesize the facts, tool outputs, and diagnostic findings gathered so far into a clear, cohesive final response. "
                    "Report what was discovered, note any pending commands awaiting user approval, and highlight any recommended next steps."
                )
                synthesis_messages = list(messages) + [
                    LLMMessage(
                        role="user",
                        content="Diagnostic tool budget reached. Synthesize the findings gathered above and answer my question directly without calling any more tools.",
                    )
                ]
                final_response = await self.provider.generate_response(
                    messages=synthesis_messages,
                    tools=[],
                    system_instruction=synthesis_instruction,
                )
                total_input_tokens += final_response.input_tokens
                total_output_tokens += final_response.output_tokens
                self.usage.record_usage(
                    model=self.model_name,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    context="chat:iteration_limit_synthesized",
                )
                synthesized_text = (final_response.content or "").strip()
                if not synthesized_text:
                    observed_tools = [m.name for m in messages if m.role == "tool" and m.name]
                    synthesized_text = (
                        f"⚠️ Reached diagnostic tool budget ({max_iterations} iterations).\n\n"
                        f"Queried tools during analysis: {', '.join(set(observed_tools)) if observed_tools else 'Cluster telemetry'}.\n"
                        "Please narrow your query or specify a target guest or node."
                    )
                else:
                    synthesized_text += "\n\n_Note: Diagnostic tool budget reached; synthesized from gathered observations._"
                self.memory.add_message(chat_id=chat_id, role="assistant", content=synthesized_text)
                return f"{synthesized_text}{self._format_llm_footer(total_tool_calls)}"
            except Exception as e:
                logger.exception("Failed during turn limit synthesis: %s", e)
                self.usage.record_usage(
                    model=self.model_name,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    context="chat:iteration_limit_fallback",
                )
                limit_msg = f"⚠️ Reached maximum function calling iterations ({max_iterations}). Please narrow your query or approve pending commands."
                self.memory.add_message(chat_id=chat_id, role="assistant", content=limit_msg)
                return f"{limit_msg}{self._format_llm_footer(total_tool_calls)}"
        finally:
            self._defer_proposals = False

    async def investigate_alert(
        self,
        check_id: str,
        issue_summary: str,
        details: Optional[Dict[str, Any]] = None,
        chat_id: str = "default",
    ) -> str:
        """Automated alert root-cause diagnosis using the configured LLM."""
        prompt = (
            f"The monitored service '{check_id}' triggered an alert with issue:\n"
            f"\"{issue_summary}\"\n\n"
            f"Additional check details: {json.dumps(details or {}, default=str)}\n\n"
            "Please investigate this issue: check live cluster status, inspect the affected guest and check state, "
            "determine the probable root cause, and provide clear, actionable troubleshooting steps or commands."
        )
        return await self.chat(
            prompt,
            chat_id=chat_id,
            context_note=f"Investigating alert for check '{check_id}'",
        )
