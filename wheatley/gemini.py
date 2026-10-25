"""Wheatley Gemini Integration - Dynamic Inventory Prompt, Tool Registry & Reasoning Loop"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

from wheatley.audit import AuditLogger
from wheatley.budget import BudgetTracker
from wheatley.checks.engine import CheckEngine
from wheatley.config import WheatleyConfig
from wheatley.tools.proxmox import ProxmoxClient

logger = logging.getLogger("wheatley.gemini")


class GeminiAgent:
    """Manages conversational reasoning, automated alert investigation, and tool calling with Gemini."""

    def __init__(
        self,
        config: WheatleyConfig,
        audit: AuditLogger,
        budget: BudgetTracker,
        check_engine: Optional[CheckEngine] = None,
        proxmox_clients: Optional[Dict[str, ProxmoxClient]] = None,
    ):
        self.config = config
        self.audit = audit
        self.budget = budget
        self.check_engine = check_engine
        self.clients = proxmox_clients or {}
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.model_name = config.budget.model_flash  # Default: gemini-3.8-flash

        self.client: Optional[Any] = None
        if GENAI_AVAILABLE and self.api_key and "xxxx" not in self.api_key:
            try:
                self.client = genai.Client(api_key=self.api_key)
                logger.info("Gemini client initialized with model '%s'", self.model_name)
            except Exception as e:
                logger.warning("Could not initialize genai.Client: %s", e)

    @property
    def is_configured(self) -> bool:
        return bool(self.client is not None and self.api_key and "xxxx" not in self.api_key)

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

        allowlist_cmds = ", ".join([f"`{cmd}`" for cmd in self.config.allowlist[:8]])

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
        logger.info("Executing Gemini tool '%s' with args %s", name, args)
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

        # Run synchronously in thread
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

        # Note: Interactive proposal confirmation flows to Telegram
        return {
            "status": "PROPOSED",
            "target": target,
            "command": command,
            "summary": summary,
            "risk": risk,
            "note": "Proposal prepared. Explain this to the user; state changes require explicit interactive button approval.",
        }

    def _get_tool_declarations(self) -> List[Any]:
        """Define schema declarations for Gemini function calling."""
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="get_cluster_status",
                        description="Query live CPU, RAM, uptime, and guest power states across all Proxmox nodes.",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
                    ),
                    types.FunctionDeclaration(
                        name="get_guest_details",
                        description="Get detailed status, IP addresses, specs, and config for a VM or LXC container.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "node": types.Schema(type=types.Type.STRING, description="Node name (e.g. cave, doug)"),
                                "vmid": types.Schema(type=types.Type.INTEGER, description="Virtual Machine or Container ID"),
                            },
                            required=["node", "vmid"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_check_states",
                        description="List all monitored health checks, their current state (OK, CRIT, WARN, MUTED), and last results.",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
                    ),
                    types.FunctionDeclaration(
                        name="run_health_check",
                        description="Execute a specific configured health check on-demand and return its live result and metrics.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "check_id": types.Schema(type=types.Type.STRING, description="Check ID (e.g. plex, frigate, node-health)"),
                            },
                            required=["check_id"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_recent_audit_logs",
                        description="Retrieve recent security, authorization, and operational audit events.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "limit": types.Schema(type=types.Type.INTEGER, description="Max logs to return (default 10)"),
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="propose_action",
                        description="Submit an action proposal for user approval before executing any mutating command (start, stop, restart, config change).",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "target": types.Schema(type=types.Type.STRING, description="Target host or guest (e.g. cave/108)"),
                                "command": types.Schema(type=types.Type.STRING, description="Exact shell command to execute"),
                                "summary": types.Schema(type=types.Type.STRING, description="Plain English summary of what will happen"),
                                "risk": types.Schema(type=types.Type.STRING, description="Risk level: LOW, MEDIUM, or HIGH"),
                            },
                            required=["target", "command", "summary", "risk"],
                        ),
                    ),
                ]
            )
        ]

    async def chat(self, prompt: str, context_note: Optional[str] = None) -> str:
        """Run the conversational reasoning loop with Gemini 3.8 Flash."""
        if not self.is_configured:
            return (
                "⚠️ *Gemini AI is unconfigured.*\n\n"
                "To enable conversational reasoning and automated investigations:\n"
                "1. Add your Google AI Studio API key to `/etc/wheatley/secrets.env` (`GEMINI_API_KEY=\"AIza...\"`)\n"
                "2. Restart Wheatley (`systemctl restart wheatley`)"
            )

        # Budget safety check
        can_call, reason = self.budget.can_call_llm()
        if not can_call:
            return f"⏸️ *AI Reasoning Paused:*\n{reason}"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync_chat_loop, prompt, context_note)

    def _sync_chat_loop(self, prompt: str, context_note: Optional[str] = None) -> str:
        """Synchronous multi-turn tool calling loop executed in thread."""
        user_content = prompt
        if context_note:
            user_content = f"Context: {context_note}\n\nQuestion: {prompt}"

        system_instruction = self.build_system_prompt()
        tools = self._get_tool_declarations()

        contents: List[Any] = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_content)],
            )
        ]

        total_input_tokens = 0
        total_output_tokens = 0
        iterations = 0
        max_iterations = self.config.budget.max_tool_iterations

        while iterations < max_iterations:
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=tools,
                temperature=0.2,
            )

            response = None
            for attempt in range(1, 4):
                try:
                    response = self.client.models.generate_content(
                        model=self.model_name,
                        contents=contents,
                        config=config,
                    )
                    break
                except Exception as e:
                    err_str = str(e)
                    if ("503" in err_str or "429" in err_str or "UNAVAILABLE" in err_str) and attempt < 3:
                        import time
                        wait_sec = attempt * 2
                        logger.warning("Gemini 503/429 spike on attempt %d/3, retrying in %ds...", attempt, wait_sec)
                        time.sleep(wait_sec)
                        continue
                    logger.exception("Gemini API call failed: %s", e)
                    return f"❌ *Gemini API Error:* `{e}`"

            if not response:
                return "❌ *Gemini API Error:* No response received after retries."

            # Track tokens
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                total_input_tokens += getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                total_output_tokens += getattr(response.usage_metadata, "candidates_token_count", 0) or 0

            # Check if model requested function calls
            function_calls = response.function_calls
            if not function_calls:
                # Model provided final text response
                self.budget.record_usage(
                    model=self.model_name,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    context=f"chat:{prompt[:30]}",
                )
                return response.text or "I processed your request, but received an empty text response."

            # Append assistant message with function call
            if response.candidates and response.candidates[0].content:
                contents.append(response.candidates[0].content)

            # Execute all function calls and build tool response parts
            tool_parts = []
            for call in function_calls:
                tool_result = self._execute_tool(call.name, dict(call.args or {}))
                tool_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"result": tool_result},
                    )
                )

            contents.append(types.Content(role="tool", parts=tool_parts))

        # Iteration cap reached
        self.budget.record_usage(
            model=self.model_name,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            context="chat:iteration_limit",
        )
        return "⚠️ Reached maximum function calling iterations (10) without completing. Please narrow your query."

    async def investigate_alert(
        self,
        check_id: str,
        issue_summary: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Automated alert root-cause diagnosis using Gemini 3.8 Flash."""
        prompt = (
            f"The monitored service '{check_id}' triggered an alert with issue:\n"
            f"\"{issue_summary}\"\n\n"
            f"Additional check details: {json.dumps(details or {}, default=str)}\n\n"
            "Please investigate this issue: check live cluster status, inspect the affected guest and check state, "
            "determine the probable root cause, and provide clear, actionable troubleshooting steps or commands."
        )
        return await self.chat(prompt, context_note=f"Investigating alert for check '{check_id}'")
