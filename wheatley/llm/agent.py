"""Wheatley LLM Agent - Multi-Turn Reasoning, Tool Calling, and Automated Alert Investigation"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import logging
import re
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from wheatley.approvals import ApprovalStore
from wheatley.audit import AuditLogger
from wheatley.checks.engine import CheckEngine
from wheatley.config import WheatleyConfig
from wheatley.llm.base import BaseLLMProvider, LLMMessage, ToolCall, ToolDefinition
from wheatley.llm.memory import ConversationStore
from wheatley.llm.providers import get_llm_provider
from wheatley.llm.tools import AgentToolbox, CommandExecutionHalted
from wheatley.llm.usage import UsageTracker
from wheatley.tools.proxmox import ProxmoxClient
from wheatley.tools.shell import ShellExecutor

logger = logging.getLogger("wheatley.llm.agent")

_turn_proposals_var: ContextVar[Optional[List[Any]]] = ContextVar("_turn_proposals_var", default=None)

__all__ = ["LLMAgent", "CommandExecutionHalted"]


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
        memory: Optional[ConversationStore] = None,
        approvals: Optional[ApprovalStore] = None,
        proposal_sink: Optional[Callable[[Any], Coroutine[Any, Any, None]]] = None,
    ):
        self.config = config
        self.audit = audit
        self.check_engine = check_engine
        self.clients = proxmox_clients or {}
        self._shell = shell or (check_engine.shell if check_engine else ShellExecutor(self.config))

        db_ref = getattr(audit, "db", getattr(audit, "db_path", "/var/lib/wheatley/wheatley.db"))
        self.memory = memory or ConversationStore(db_ref)
        self.approvals = approvals or ApprovalStore(db_ref)
        self.proposal_sink = proposal_sink
        self.pending_proposals: List[Any] = []
        self._defer_proposals: bool = False
        self._active_chat_turns: int = 0

        self.usage = usage if usage is not None else UsageTracker(self.audit)
        self.provider = provider if provider is not None else get_llm_provider(self.config.llm)

        self.toolbox = AgentToolbox(
            config=self.config,
            audit=self.audit,
            approvals=self.approvals,
            shell=self._shell,
            check_engine=self.check_engine,
            proxmox_clients=self.clients,
            on_proposal=self._dispatch_proposal,
        )
        self._tools: List[ToolDefinition] = self.toolbox.tool_definitions

    @property
    def shell(self) -> ShellExecutor:
        return self._shell

    @shell.setter
    def shell(self, value: ShellExecutor) -> None:
        self._shell = value
        if hasattr(self, "toolbox"):
            self.toolbox.shell = value

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
        return self.toolbox.tool_definitions

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

    def _sync_toolbox_refs(self) -> None:
        self.toolbox.config = self.config
        self.toolbox.audit = self.audit
        self.toolbox.approvals = self.approvals
        self.toolbox.shell = self._shell
        self.toolbox.check_engine = self.check_engine
        self.toolbox.clients = self.clients

    def _execute_tool(self, name: str, args: Dict[str, Any]) -> Any:
        self._sync_toolbox_refs()
        return self.toolbox.execute(name, args)

    def _tool_get_cluster_status(self) -> Dict[str, Any]:
        self._sync_toolbox_refs()
        return self.toolbox.get_cluster_status()

    def _tool_get_guest_details(self, node: str, vmid: int) -> Dict[str, Any]:
        self._sync_toolbox_refs()
        return self.toolbox.get_guest_details(node, vmid)

    def _tool_get_check_states(self) -> Dict[str, Any]:
        self._sync_toolbox_refs()
        return self.toolbox.get_check_states()

    async def _tool_run_health_check(self, check_id: str) -> Dict[str, Any]:
        self._sync_toolbox_refs()
        return await self.toolbox.run_health_check(check_id)

    async def _tool_run_host_command(
        self,
        node: str,
        command: str,
        summary: Optional[str] = None,
        justification: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._sync_toolbox_refs()
        return await self.toolbox.run_host_command(node, command, summary=summary, justification=justification)

    async def _tool_run_guest_command(
        self,
        node: str,
        vmid: int,
        command: str,
        summary: Optional[str] = None,
        justification: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._sync_toolbox_refs()
        return await self.toolbox.run_guest_command(node, vmid, command, summary=summary, justification=justification)

    def _tool_get_audit_logs(self, limit: int = 10) -> Dict[str, Any]:
        self._sync_toolbox_refs()
        return self.toolbox.get_audit_logs(limit)

    def _tool_propose_action(self, target: str, command: str, summary: str, risk: str) -> Dict[str, Any]:
        self._sync_toolbox_refs()
        return self.toolbox.propose_action(target, command, summary, risk)

    def _tool_propose_check_config_update(
        self,
        check_id: str,
        action: str,
        summary: str,
        key: Optional[str] = None,
        values: Optional[List[str]] = None,
        value: Any = None,
    ) -> Dict[str, Any]:
        self._sync_toolbox_refs()
        return self.toolbox.propose_check_config_update(
            check_id=check_id,
            action=action,
            summary=summary,
            key=key,
            values=values,
            value=value,
        )

    @staticmethod
    def _proposal_supersede_key(prop: Any) -> Optional[Tuple[str, str, str]]:
        """Return a deduplication key for proposals that should supersede earlier drafts in the same turn."""
        if getattr(prop, "proposal_type", None) == "CONFIG_UPDATE":
            try:
                data = json.loads(getattr(prop, "command", "") or "{}")
                check_id = data.get("check_id")
                if check_id:
                    return ("CONFIG_UPDATE", str(getattr(prop, "target", "")), str(check_id))
            except Exception:
                pass
        return None

    def _replace_or_append_proposal(self, prop_list: List[Any], new_prop: Any) -> None:
        key = self._proposal_supersede_key(new_prop)
        if key is not None:
            for idx, existing in enumerate(prop_list):
                if self._proposal_supersede_key(existing) == key:
                    if getattr(existing, "id", None) and getattr(existing, "id", None) != getattr(new_prop, "id", None):
                        try:
                            self.approvals.resolve_proposal(existing.id, "DENY", user_id=0)
                        except Exception:
                            pass
                    prop_list[idx] = new_prop
                    return
        prop_list.append(new_prop)

    def _dispatch_proposal(self, prop: Any) -> None:
        turn_props = _turn_proposals_var.get()
        if turn_props is not None:
            self._replace_or_append_proposal(turn_props, prop)
            self._replace_or_append_proposal(self.pending_proposals, prop)
            return

        if self._defer_proposals:
            self._replace_or_append_proposal(self.pending_proposals, prop)
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

        turn_proposals: List[Any] = []
        token = _turn_proposals_var.set(turn_proposals)
        if self._active_chat_turns == 0:
            self.pending_proposals = []
        self._active_chat_turns += 1
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
                        self._sync_toolbox_refs()
                        raw_result = await self.toolbox.execute_async(call.name, call.arguments)
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
            _turn_proposals_var.reset(token)
            self._active_chat_turns = max(0, self._active_chat_turns - 1)
            if self._active_chat_turns == 0:
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
