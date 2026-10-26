"""Wheatley LLM Agent - Multi-Turn Reasoning, Tool Calling, and Automated Alert Investigation"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from wheatley.audit import AuditLogger
from wheatley.checks.engine import CheckEngine
from wheatley.config import WheatleyConfig
from wheatley.llm.base import BaseLLMProvider, LLMMessage, ToolCall, ToolDefinition
from wheatley.llm.memory import ConversationStore
from wheatley.llm.providers import get_llm_provider
from wheatley.llm.usage import UsageTracker
from wheatley.tools.allowlist import is_command_allowed
from wheatley.tools.proxmox import ProxmoxClient
from wheatley.tools.shell import ShellExecutor

logger = logging.getLogger("wheatley.llm.agent")


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
                description="Query live CPU, RAM, uptime, and guest power states across all Proxmox nodes.",
                parameters={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="get_guest_details",
                description="Get detailed status, IP addresses, specs, and config for a VM or LXC container.",
                parameters={
                    "type": "object",
                    "properties": {
                        "node": {"type": "string", "description": "Node name (e.g. cave, doug)"},
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
                        "check_id": {"type": "string", "description": "Check ID (e.g. plex, frigate, node-health)"},
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
                    "Execute a read-only diagnostics command directly on a Proxmox host node (e.g. 'cave'). "
                    "Permitted commands include: uptime, free -m, df -h, journalctl -n 20, systemctl status <unit>, "
                    "pvesm status, smartctl -H <device>, sensors -j, zpool status, etc. "
                    "Any non-allowlisted or mutating command is blocked and automatically converted to an action proposal."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "node": {"type": "string", "description": "Target Proxmox node name (e.g. cave, doug)"},
                        "command": {"type": "string", "description": "Shell command to execute on the host"},
                    },
                    "required": ["node", "command"],
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
                        "node": {"type": "string", "description": "Host node where the guest resides (e.g. cave)"},
                        "vmid": {"type": "integer", "description": "Virtual Machine or Container ID (e.g. 100, 102)"},
                        "command": {"type": "string", "description": "Shell command to execute inside the guest"},
                    },
                    "required": ["node", "vmid", "command"],
                },
            ),
            ToolDefinition(
                name="propose_action",
                description="Submit an action proposal for user approval before executing any mutating command (start, stop, restart, config change).",
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Target host or guest (e.g. cave/108)"},
                        "command": {"type": "string", "description": "Exact shell command to execute"},
                        "summary": {"type": "string", "description": "Plain English summary of what will happen"},
                        "risk": {"type": "string", "description": "Risk level: LOW, MEDIUM, or HIGH"},
                    },
                    "required": ["target", "command", "summary", "risk"],
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
            "5. Host & Guest Diagnostic Execution: You have access to 'run_host_command' and 'run_guest_command' to execute read-only diagnostic commands directly (e.g. docker ps, docker logs, uptime, free, df, systemctl status, journalctl, sensors, smartctl). Non-allowlisted or state-changing commands will automatically produce an interactive approval proposal.",
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
                return self._tool_run_host_command(args.get("node", ""), args.get("command", ""))
            elif name == "run_guest_command":
                return self._tool_run_guest_command(
                    args.get("node", ""), int(args.get("vmid", 0)), args.get("command", "")
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
            else:
                return {"error": f"Unknown tool: '{name}'"}
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

    async def _tool_run_host_command(self, node: str, command: str) -> Dict[str, Any]:
        allowed, reason = is_command_allowed(command, self.config.allowlist)
        if not allowed:
            logger.info("Host command '%s' on %s blocked by allowlist (%s); proposing action", command, node, reason)
            proposal = self._tool_propose_action(
                target=node,
                command=command,
                summary=f"Run command on host {node}: {command}",
                risk="HIGH" if any(w in command for w in ("rm", "dd", "mkfs", "reboot", "shutdown")) else "MEDIUM",
            )
            return {
                "status": "REQUIRES_APPROVAL",
                "message": f"Command '{command}' is not in the read-only allowlist ({reason}). An interactive approval proposal has been created.",
                "proposal": proposal,
            }

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

    async def _tool_run_guest_command(self, node: str, vmid: int, command: str) -> Dict[str, Any]:
        allowed, reason = is_command_allowed(command, self.config.allowlist)
        target = f"{node}/{vmid}"
        if not allowed:
            logger.info("Guest command '%s' on %s blocked by allowlist (%s); proposing action", command, target, reason)
            proposal = self._tool_propose_action(
                target=target,
                command=command,
                summary=f"Run command on guest {target}: {command}",
                risk="HIGH" if any(w in command for w in ("rm", "dd", "mkfs", "reboot", "shutdown")) else "MEDIUM",
            )
            return {
                "status": "REQUIRES_APPROVAL",
                "message": f"Command '{command}' is not in the read-only allowlist ({reason}). An interactive approval proposal has been created.",
                "proposal": proposal,
            }

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

        return {
            "status": "PROPOSED",
            "target": target,
            "command": command,
            "summary": summary,
            "risk": risk,
            "note": "Proposal prepared. Explain this to the user; state changes require explicit interactive button approval.",
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
        history: List[LLMMessage] = []
        if max_turns > 0:
            history = self.memory.get_history(chat_id=chat_id, max_turns=max_turns)

        # Build initial message chain
        current_user_msg = LLMMessage(role="user", content=user_content)
        messages: List[LLMMessage] = list(history) + [current_user_msg]

        # Record this user message to SQLite
        self.memory.add_message(chat_id=chat_id, role="user", content=user_content)

        system_instruction = self.build_system_prompt()
        total_input_tokens = 0
        total_output_tokens = 0
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
                return final_text

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
            for call in response.tool_calls:
                raw_result = self._execute_tool(call.name, call.arguments)
                if asyncio.iscoroutine(raw_result):
                    raw_result = await raw_result
                tool_result = self._truncate_tool_result(raw_result)
                messages.append(
                    LLMMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=tool_result,
                    )
                )

            iterations += 1

        # Iteration cap reached
        self.usage.record_usage(
            model=self.model_name,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            context="chat:iteration_limit",
        )
        limit_msg = f"⚠️ Reached maximum function calling iterations ({max_iterations}) without completing. Please narrow your query."
        self.memory.add_message(chat_id=chat_id, role="assistant", content=limit_msg)
        return limit_msg

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
