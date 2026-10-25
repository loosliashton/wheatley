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
from wheatley.llm.providers import get_llm_provider
from wheatley.llm.usage import UsageTracker
from wheatley.tools.proxmox import ProxmoxClient

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
        **kwargs: Any,  # Absorb legacy kwargs like budget=...
    ):
        self.config = config
        self.audit = audit
        self.check_engine = check_engine
        self.clients = proxmox_clients or {}

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
            "3. Conciseness: Homelab administrators read your messages on mobile via Telegram. Keep answers structured, insightful, and concise. Use bolding and markdown code blocks effectively.",
            "4. Grounded in facts: Base all diagnostic answers on tool output. If a service is down, report the exact status code, latency, or error message.",
        ]
        return "\n".join(lines)

    def _execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
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

    def _tool_run_health_check(self, check_id: str) -> Dict[str, Any]:
        if not self.check_engine:
            return {"error": "Check engine not active"}
        if check_id not in self.check_engine.checks:
            return {"error": f"Check '{check_id}' not found"}

        # Run synchronously in thread if called from sync context or await if in async
        try:
            loop = asyncio.get_running_loop()
            # If inside running loop, we can schedule execution
            res = asyncio.run_coroutine_threadsafe(
                self.check_engine.execute_check(check_id), loop
            ).result(timeout=30)
        except RuntimeError:
            res = asyncio.run(self.check_engine.execute_check(check_id))

        return {
            "check_id": res.check_id,
            "status": res.status.value,
            "summary": res.summary,
            "metrics": res.metrics,
            "details": res.details,
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

    async def chat(self, prompt: str, context_note: Optional[str] = None) -> str:
        """Run the multi-turn conversational reasoning loop."""
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

        system_instruction = self.build_system_prompt()
        messages: List[LLMMessage] = [LLMMessage(role="user", content=user_content)]

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

            # If no tool calls requested, return the model's text response
            if not response.tool_calls:
                self.usage.record_usage(
                    model=self.model_name,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    context=f"chat:{prompt[:30]}",
                )
                return response.content or "I processed your request, but received an empty text response."

            # Append assistant message with requested tool calls
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                    raw=response.raw,
                )
            )

            # Execute tool calls and append results
            for call in response.tool_calls:
                tool_result = self._execute_tool(call.name, call.arguments)
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
        return f"⚠️ Reached maximum function calling iterations ({max_iterations}) without completing. Please narrow your query."

    async def investigate_alert(
        self,
        check_id: str,
        issue_summary: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Automated alert root-cause diagnosis using the configured LLM."""
        prompt = (
            f"The monitored service '{check_id}' triggered an alert with issue:\n"
            f"\"{issue_summary}\"\n\n"
            f"Additional check details: {json.dumps(details or {}, default=str)}\n\n"
            "Please investigate this issue: check live cluster status, inspect the affected guest and check state, "
            "determine the probable root cause, and provide clear, actionable troubleshooting steps or commands."
        )
        return await self.chat(prompt, context_note=f"Investigating alert for check '{check_id}'")
