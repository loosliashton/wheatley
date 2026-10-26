"""Telegram Bot Adapter for Wheatley"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from wheatley.approvals import ApprovalStore
from wheatley.audit import AuditLogger
from wheatley.chat.base import ChatInterface
from wheatley.chat.formatter import (
    markdown_to_telegram_html,
    strip_html_tags,
    split_telegram_message,
)
from wheatley.config import TelegramConfig
from wheatley.tools.shell import ShellExecutor

logger = logging.getLogger("wheatley.chat.telegram")


class TypingIndicator:
    """Async context manager that continuously emits Telegram 'typing' actions every 4s.

    Telegram's sendChatAction('typing') expires automatically after 5 seconds.
    This keeps the typing indicator active for long-running reasoning loops and commands.
    """

    def __init__(self, chat: Optional[Any], interval: float = 4.0):
        self.chat = chat
        self.interval = interval
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "TypingIndicator":
        if self.chat and hasattr(self.chat, "send_action"):
            self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._task:
            self._stop.set()
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.chat.send_action("typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass


class TelegramBot(ChatInterface):
    """Telegram Chat Adapter with user-ID whitelisting and interactive buttons."""

    def __init__(
        self,
        config: TelegramConfig,
        audit: AuditLogger,
        status_provider: Optional[Callable[[], Coroutine[Any, Any, str]]] = None,
        discovery_provider: Optional[Callable[[], Coroutine[Any, Any, str]]] = None,
        check_engine: Optional[Any] = None,
        gemini_agent: Optional[Any] = None,
        llm_agent: Optional[Any] = None,
        budget: Optional[Any] = None,
        shell: Optional[ShellExecutor] = None,
        approvals: Optional[ApprovalStore] = None,
        **kwargs: Any,
    ):
        self.config = config
        self.audit = audit
        self.status_provider = status_provider
        self.discovery_provider = discovery_provider
        self.check_engine = check_engine
        self.agent = llm_agent or gemini_agent
        self.budget = budget
        self.shell = shell or getattr(self.agent, "shell", None)
        self.approvals = (
            approvals
            or kwargs.get("approvals")
            or getattr(self.agent, "approvals", None)
            or ApprovalStore(getattr(audit, "db_path", "/var/lib/wheatley/wheatley.db"))
        )
        self.allowed_users: Set[int] = set(config.allowed_user_ids)
        self.primary_chat_id: Optional[int] = config.allowed_user_ids[0] if config.allowed_user_ids else None
        self.app: Application = ApplicationBuilder().token(config.bot_token).build()
        self.proposal_callback: Optional[Callable[[str, bool, int], Coroutine[Any, Any, None]]] = None
        self._custom_commands: Dict[str, Callable[..., Coroutine[Any, Any, None]]] = {}
        self._register_internal_handlers()

    @property
    def gemini_agent(self) -> Optional[Any]:
        return self.agent

    @gemini_agent.setter
    def gemini_agent(self, value: Any) -> None:
        self.agent = value

    @property
    def llm_agent(self) -> Optional[Any]:
        return self.agent

    @llm_agent.setter
    def llm_agent(self, value: Any) -> None:
        self.agent = value

    def _is_authorized(self, update: Update) -> bool:
        if not update.effective_user:
            return False
        uid = update.effective_user.id
        if uid not in self.allowed_users:
            self.audit.log_event(
                event_type="AUTH",
                source=f"tg:{uid}",
                action="unauthorized_access_attempt",
                status="DENIED",
                details=f"Username: {update.effective_user.username}",
            )
            logger.warning("Dropped message from unauthorized Telegram user_id: %s", uid)
            return False
        return True

    def _register_internal_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("checks", self._cmd_checks))
        self.app.add_handler(CommandHandler("mute", self._cmd_mute))
        self.app.add_handler(CommandHandler("unmute", self._cmd_unmute))
        self.app.add_handler(CommandHandler("usage", self._cmd_usage))
        self.app.add_handler(CommandHandler("investigate", self._cmd_investigate))
        self.app.add_handler(CommandHandler("allowlist", self._cmd_allowlist))
        self.app.add_handler(CommandHandler("discover", self._cmd_discover))
        self.app.add_handler(CommandHandler("audit", self._cmd_audit))
        self.app.add_handler(CommandHandler("reset", self._cmd_reset))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))

    async def _send_or_reply(
        self,
        text: str,
        target: Any = None,
        chat_id: Optional[int] = None,
        reply_markup: Optional[Any] = None,
    ) -> List[Any]:
        """Converts Markdown text to Telegram HTML, splits long messages, and sends safely with fallback."""
        if not text:
            return []

        chunks = split_telegram_message(text)
        results = []

        for chunk in chunks:
            html_chunk = markdown_to_telegram_html(chunk)
            sent = False

            # 1. Attempt sending as Telegram HTML
            try:
                if target is not None:
                    res = await target.reply_text(
                        html_chunk,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                        disable_web_page_preview=True,
                    )
                else:
                    cid = chat_id or self.primary_chat_id
                    if cid is None:
                        logger.warning("Cannot send message: primary_chat_id is unset.")
                        return results
                    res = await self.app.bot.send_message(
                        chat_id=cid,
                        text=html_chunk,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                        disable_web_page_preview=True,
                    )
                results.append(res)
                sent = True
            except Exception as e:
                logger.warning("Telegram HTML send failed (%s), falling back to clean text", e)

            # 2. Fallback to clean plain text (HTML tags stripped)
            if not sent:
                clean_chunk = strip_html_tags(html_chunk)
                try:
                    if target is not None:
                        res = await target.reply_text(
                            clean_chunk,
                            parse_mode=None,
                            reply_markup=reply_markup,
                            disable_web_page_preview=True,
                        )
                    else:
                        cid = chat_id or self.primary_chat_id
                        res = await self.app.bot.send_message(
                            chat_id=cid,
                            text=clean_chunk,
                            parse_mode=None,
                            reply_markup=reply_markup,
                            disable_web_page_preview=True,
                        )
                    results.append(res)
                except Exception as ex2:
                    logger.error("Failed clean plain text send fallback: %s", ex2)

        return results

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_name = update.effective_user.first_name if update.effective_user else "Commander"
        msg = (
            f"⚙️ [SYSTEM] *Greetings, {user_name}!*\n\n"
            f"I'm *Wheatley*, your homelab monitoring & AI administration agent.\n\n"
            f"• `/status` - Check node and service health\n"
            f"• `/checks` - View all active checks & mute states\n"
            f"• `/usage` - View LLM token spend & activity\n"
            f"• `/allowlist` - Manage learned command allowlist rules\n"
            f"• `/discover` - Auto-detect services & containers\n"
            f"• `/audit` - Inspect recent action logs\n"
            f"• `/reset` - Clear conversation history & memory\n"
            f"• `/help` - See all available commands\n\n"
            f"You can also chat directly with me about any homelab questions!"
        )
        self.audit.log_event("CMD", f"tg:{update.effective_user.id}", "/start", "SUCCESS")
        await self._send_or_reply(msg, target=update.message)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        help_text = (
            "⚙️ [SYSTEM] *Wheatley Commands*\n\n"
            "/status - Cluster & guest health overview\n"
            "/checks - View all health checks and mute status\n"
            "/usage - LLM token consumption & activity\n"
            "/investigate <id> - Run AI diagnosis on a service\n"
            "/allowlist - View or revoke learned allowlist rules\n"
            "/mute <id> [mins] - Silence alerts for a check\n"
            "/unmute <id> - Re-enable alerts for a check\n"
            "/discover - Probe nodes & guests to discover active services\n"
            "/audit - Recent security and activity audit log\n"
            "/reset - Clear conversation context & start fresh\n"
            "/help - Display this manual\n\n"
            "💬 *AI Chat:* Simply send a message to ask questions or diagnose services!"
        )
        await self._send_or_reply(help_text, target=update.message)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self.status_provider:
            status_text = await self.status_provider()
            status_text = status_text.lstrip("🤖 ").lstrip("🟢 ")
        else:
            status_text = "*Wheatley Core:* Online\nNode checks initializing..."
        await self._send_or_reply(f"⚙️ [SYSTEM] {status_text}", target=update.message)

    async def _cmd_discover(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        self.audit.log_event("CMD", f"tg:{update.effective_user.id}", "/discover", "SUCCESS")
        await self._send_or_reply(
            "⚙️ [SYSTEM] *Scanning Proxmox nodes and probing guests for services...*\n_This may take a few seconds._",
            target=update.message,
        )
        if self.discovery_provider:
            try:
                async with TypingIndicator(update.effective_chat):
                    summary = await self.discovery_provider()
                summary_clean = summary.lstrip("🔍 ")
                await self._send_or_reply(f"⚙️ [SYSTEM] {summary_clean}", target=update.message)
            except Exception as e:
                logger.exception("Telegram /discover failed")
                await self._send_or_reply(f"⚙️ [SYSTEM] *Discovery failed:* `{e}`", target=update.message)
        else:
            await self._send_or_reply("⚙️ [SYSTEM] Auto-discovery provider not connected to bot.", target=update.message)

    async def _cmd_audit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        logs = self.audit.get_recent_logs(limit=8)
        if not logs:
            await self._send_or_reply("⚙️ [SYSTEM] Audit log is currently empty.", target=update.message)
            return

        lines = ["⚙️ [SYSTEM] *Recent Audit Events:*"]
        for entry in logs:
            ts = entry["timestamp"][:19].replace("T", " ")
            lines.append(
                f"• `[{ts}]` *{entry['action']}* ({entry['status']}) - `{entry['source']}`"
            )
        await self._send_or_reply("\n".join(lines), target=update.message)

    async def _cmd_checks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.check_engine:
            await self._send_or_reply("⚙️ [SYSTEM] Check engine is not connected to bot.", target=update.message)
            return

        states = self.check_engine.get_check_states()
        if not states:
            await self._send_or_reply("⚙️ [SYSTEM] No health checks currently configured.", target=update.message)
            return

        lines = [f"⚙️ [SYSTEM] *Wheatley Monitored Checks ({len(states)})*\n"]
        for s in states:
            cid = s["id"]
            ctype = s["type"]
            status = s["status"]
            is_muted = s["is_muted"]
            muted_until = s["muted_until"]

            if is_muted:
                until_str = muted_until.strftime("%H:%M UTC") if muted_until else "muted"
                icon = "🔕"
                status_str = f"MUTED (until {until_str})"
            elif status == "OK":
                icon = "🟢"
                status_str = "OK"
            elif status == "WARN":
                icon = "⚠️"
                status_str = "WARN"
            elif status == "CRIT":
                icon = "🚨"
                status_str = f"CRIT ({s['consecutive_failures']} failures)"
            else:
                icon = "⚪️"
                status_str = status

            lines.append(f"{icon} `{cid}` ({ctype}): *{status_str}*")

        lines.append("\n_Use `/mute <id> [mins]` to silence alerts, or `/unmute <id>`._")
        await self._send_or_reply("\n".join(lines), target=update.message)

    async def _cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.check_engine:
            await self._send_or_reply("⚙️ [SYSTEM] Check engine is not connected.", target=update.message)
            return

        args = context.args or []
        if not args:
            states = self.check_engine.get_check_states()
            check_ids = [f"`{s['id']}`" for s in states]
            msg = (
                "⚙️ [SYSTEM] *Usage:* `/mute <check_id> [minutes]`\n"
                "Example: `/mute plex 120` (default: 60 minutes)\n\n"
                f"Available checks: {', '.join(check_ids) if check_ids else 'None'}"
            )
            await self._send_or_reply(msg, target=update.message)
            return

        check_id = args[0]
        minutes = 60
        if len(args) > 1:
            try:
                minutes = int(args[1])
            except ValueError:
                await self._send_or_reply("⚙️ [SYSTEM] Minutes must be a whole number, e.g. `/mute plex 60`", target=update.message)
                return

        until = self.check_engine.mute_check(check_id, minutes)
        if not until:
            await self._send_or_reply(f"⚙️ [SYSTEM] Check `{check_id}` not found.", target=update.message)
            return

        until_str = until.strftime("%Y-%m-%d %H:%M:%S UTC")
        await self._send_or_reply(
            f"🔔 [MONITOR] 🔕 *Muted check* `{check_id}` for {minutes} minutes.\nAlerts silenced until `{until_str}`.",
            target=update.message,
        )

    async def _cmd_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.check_engine:
            await self._send_or_reply("⚙️ [SYSTEM] Check engine is not connected.", target=update.message)
            return

        args = context.args or []
        if not args:
            await self._send_or_reply("⚙️ [SYSTEM] *Usage:* `/unmute <check_id>`", target=update.message)
            return

        check_id = args[0]
        success = self.check_engine.unmute_check(check_id)
        if success:
            await self._send_or_reply(f"🔔 [MONITOR] 🔔 *Unmuted check* `{check_id}`. Alerts are re-enabled.", target=update.message)
        else:
            await self._send_or_reply(f"⚙️ [SYSTEM] Check `{check_id}` not found.", target=update.message)

    async def _cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self.agent and hasattr(self.agent, "usage"):
            msg = self.agent.usage.format_usage_markdown(
                provider=getattr(self.agent, "provider_name", "llm"),
                model=getattr(self.agent, "model_name", "default"),
            )
            await self._send_or_reply(f"⚙️ [SYSTEM] {msg}", target=update.message)
        elif self.budget:
            msg = self.budget.format_usage_markdown()
            await self._send_or_reply(f"⚙️ [SYSTEM] {msg}", target=update.message)
        else:
            await self._send_or_reply("⚙️ [SYSTEM] Token usage tracker is not connected.", target=update.message)

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        chat_id = str(update.effective_chat.id) if update.effective_chat else "default"
        if self.agent and hasattr(self.agent, "reset_conversation"):
            deleted = self.agent.reset_conversation(chat_id)
            await self._send_or_reply(f"⚙️ [SYSTEM] *Conversation memory cleared.* ({deleted} turns removed)", target=update.message)
        else:
            await self._send_or_reply("⚙️ [SYSTEM] Conversation reset.", target=update.message)

    async def _cmd_investigate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        args = context.args or []
        if not args:
            await self._send_or_reply("⚙️ [SYSTEM] *Usage:* `/investigate <check_id>` (e.g. `/investigate plex`)", target=update.message)
            return

        check_id = args[0]
        if not self.agent or not self.agent.is_configured:
            await self._send_or_reply("⚙️ [SYSTEM] LLM is not configured.", target=update.message)
            return

        model_name = getattr(self.agent, "model_name", "LLM")
        chat_id = str(update.effective_chat.id) if update.effective_chat else "default"
        await self._send_or_reply(
            f"⚙️ [SYSTEM] *Investigating `{check_id}` with {model_name}...*",
            target=update.message,
        )
        async with TypingIndicator(update.effective_chat):
            diagnosis = await self.agent.investigate_alert(
                check_id=check_id,
                issue_summary=f"Manual investigation triggered for {check_id}",
                chat_id=chat_id,
            )
        await self._send_or_reply(f"🤖 *Wheatley Diagnosis ({check_id}):*\n\n{diagnosis}", target=update.message)
        target_cid = update.effective_chat.id if update.effective_chat else None
        await self._dispatch_agent_proposals(chat_id=target_cid)

    async def _cmd_allowlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        args = context.args or []
        if not args:
            rules = self.approvals.list_user_rules()
            if not rules:
                await self._send_or_reply(
                    "⚙️ [SYSTEM] *Active User Allowlist Rules*\n\n"
                    "No custom allowlist rules configured.\n"
                    "Commands requested during chat or diagnostics can be approved to Run Once or permanently added here.",
                    target=update.message,
                )
                return

            lines = [f"⚙️ [SYSTEM] *Active User Allowlist Rules ({len(rules)})*\n"]
            for r in rules:
                created = r.created_at.strftime("%Y-%m-%d %H:%M")
                lines.append(f"• *#{r.id}* on `{r.target}`: `{r.pattern}`\n  _Added {created}_")
            lines.append("\n_To remove a rule: `/allowlist revoke <id>`_")
            await self._send_or_reply("\n".join(lines), target=update.message)
            return

        subcmd = args[0].lower()
        if subcmd == "revoke" and len(args) > 1:
            try:
                rule_id = int(args[1])
            except ValueError:
                await self._send_or_reply("⚙️ [SYSTEM] Rule ID must be an integer, e.g. `/allowlist revoke 1`", target=update.message)
                return

            if self.approvals.revoke_user_rule(rule_id):
                self.audit.log_event("ALLOWLIST", f"tg:{update.effective_user.id}", f"revoke:{rule_id}", "REVOKED")
                await self._send_or_reply(f"⚙️ [SYSTEM] *Revoked allowlist rule #{rule_id}.*", target=update.message)
            else:
                await self._send_or_reply(f"⚙️ [SYSTEM] Allowlist rule #{rule_id} not found.", target=update.message)
        else:
            await self._send_or_reply("⚙️ [SYSTEM] *Usage:* `/allowlist` (list rules) or `/allowlist revoke <id>` (remove rule)", target=update.message)

    async def _execute_proposal_command(self, prop: Any) -> str:
        if not prop:
            return "❌ Proposal details not found."

        if getattr(prop, "proposal_type", "") == "CONFIG_UPDATE":
            try:
                import json
                from pathlib import Path
                import yaml as pyyaml
                from wheatley.config import CheckConfig
                from wheatley.tools.config_editor import apply_checks_file_update

                payload = json.loads(prop.command)
                filepath = Path(payload["file"])
                new_yaml = payload["new_yaml"]
                backup_path = apply_checks_file_update(filepath, new_yaml)

                if self.check_engine:
                    raw_data = pyyaml.safe_load(new_yaml)
                    raw_list = raw_data if isinstance(raw_data, list) else raw_data.get("checks", [])
                    new_configs = [CheckConfig.model_validate(c) for c in raw_list]
                    self.check_engine.reload_checks(new_configs)

                return (
                    f"⚙️ [SYSTEM] *Updated `{filepath.name}` successfully.*\n"
                    f"• *Backup saved:* `{backup_path.name}`\n"
                    f"• *Check Engine:* Reloaded with active configuration."
                )
            except Exception as e:
                logger.exception("Failed to apply config update: %s", e)
                return f"⚙️ [SYSTEM] *Failed to apply config update:* `{e}`"

        executor = self.shell or (getattr(self.agent, "shell", None) if self.agent else None)
        if not executor:
            return "⚙️ [SYSTEM] Shell executor not available on bot."

        target = prop.target.strip()
        command = prop.command.strip()
        try:
            if "/" in target:
                parts = target.split("/", 1)
                node = parts[0]
                vmid = int(parts[1])
                res = await executor.execute_in_guest(node, vmid, command, timeout=30)
            else:
                res = await executor.execute_on_host(target, command, timeout=30)

            output = res.stdout.strip() or res.stderr.strip() or "(no output)"
            return (
                f"⚙️ [SYSTEM] *Output on `{target}` (exit code {res.exit_code}):*\n"
                f"```bash\n{output}\n```"
            )
        except Exception as e:
            logger.exception("Failed to execute proposal command: %s", e)
            return f"⚙️ [SYSTEM] *Execution error on `{target}`:* `{e}`"

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not self._is_authorized(update):
            return
        try:
            await query.answer()
        except Exception as e:
            logger.debug("Failed to answer callback query: %s", e)

        data = query.data or ""
        user_id = update.effective_user.id

        if data.startswith("allow_once:"):
            proposal_id = data.split(":", 1)[1]
            success, msg = self.approvals.resolve_proposal(proposal_id, "ALLOW_ONCE", user_id)
            if not success:
                await query.edit_message_reply_markup(reply_markup=None)
                await self._send_or_reply(f"⚙️ [SYSTEM] *Could not execute:* {msg}", target=query.message)
                return

            prop = self.approvals.get_proposal(proposal_id)
            self.audit.log_event("ALLOWLIST", f"tg:{user_id}", f"allow_once:{proposal_id}", "APPROVED", details={"cmd": prop.command if prop else ""})
            await query.edit_message_reply_markup(reply_markup=None)
            await self._send_or_reply(f"⚙️ [SYSTEM] *Executing once on `{prop.target if prop else 'target'}`...*", target=query.message)
            async with TypingIndicator(query.message.chat if query.message else None):
                res = await self._execute_proposal_command(prop)
            self.approvals.record_execution(proposal_id, res[:500])

            agent_resumed = False
            # Resume reasoning loop with the approved command's output so Wheatley completes the inquiry
            if self.agent and getattr(self.agent, "is_configured", False) and prop:
                chat_id = str(query.message.chat_id) if query.message else "default"
                continuation = (
                    f"Command '{prop.command}' on target '{prop.target}' was approved and executed.\n"
                    f"Execution output:\n{res}\n\n"
                    "Please continue your analysis and answer my previous question based on this output."
                )
                try:
                    async with TypingIndicator(query.message.chat if query.message else None):
                        followup = await self.agent.chat(continuation, chat_id=chat_id)
                    await self._send_or_reply(followup, target=query.message)
                    target_cid = query.message.chat_id if query.message else None
                    await self._dispatch_agent_proposals(chat_id=target_cid)
                    agent_resumed = True
                except Exception as e:
                    logger.exception("Agent failed to synthesize proposal continuation: %s", e)

            if not agent_resumed:
                await self._send_or_reply(res, target=query.message)

        elif data.startswith("allow_always:"):
            proposal_id = data.split(":", 1)[1]
            success, msg = self.approvals.resolve_proposal(proposal_id, "ALLOW_ALWAYS", user_id)
            if not success:
                await query.edit_message_reply_markup(reply_markup=None)
                await self._send_or_reply(f"⚙️ [SYSTEM] *Could not save rule:* {msg}", target=query.message)
                return

            prop = self.approvals.get_proposal(proposal_id)
            self.audit.log_event("ALLOWLIST", f"tg:{user_id}", f"allow_always:{proposal_id}", "ADDED", details={"pattern": prop.pattern if prop else "", "target": prop.target if prop else ""})
            await query.edit_message_reply_markup(reply_markup=None)
            target_str = prop.target if prop else "target"
            pattern_str = prop.pattern if prop else "rule"
            await self._send_or_reply(f"⚙️ [SYSTEM] *Added rule `{pattern_str}` to allowlist for `{target_str}`.*\nExecuting command now...", target=query.message)
            async with TypingIndicator(query.message.chat if query.message else None):
                res = await self._execute_proposal_command(prop)
            self.approvals.record_execution(proposal_id, res[:500])

            agent_resumed = False
            # Resume reasoning loop with the approved command's output so Wheatley completes the inquiry
            if self.agent and getattr(self.agent, "is_configured", False) and prop:
                chat_id = str(query.message.chat_id) if query.message else "default"
                continuation = (
                    f"Command rule '{prop.pattern}' on target '{prop.target}' was permanently allowed and executed.\n"
                    f"Execution output:\n{res}\n\n"
                    "Please continue your analysis and answer my previous question based on this output."
                )
                try:
                    async with TypingIndicator(query.message.chat if query.message else None):
                        followup = await self.agent.chat(continuation, chat_id=chat_id)
                    await self._send_or_reply(followup, target=query.message)
                    target_cid = query.message.chat_id if query.message else None
                    await self._dispatch_agent_proposals(chat_id=target_cid)
                    agent_resumed = True
                except Exception as e:
                    logger.exception("Agent failed to synthesize proposal continuation: %s", e)

            if not agent_resumed:
                await self._send_or_reply(res, target=query.message)

        elif data.startswith("approve:"):
            proposal_id = data.split(":", 1)[1]
            success, msg = self.approvals.resolve_proposal(proposal_id, "APPROVE", user_id)
            if not success:
                await query.edit_message_reply_markup(reply_markup=None)
                await self._send_or_reply(f"❌ *Approval failed:* {msg}", target=query.message)
                return

            prop = self.approvals.get_proposal(proposal_id)
            self.audit.log_event("PROPOSAL", f"tg:{user_id}", f"approve:{proposal_id}", "APPROVED")
            await query.edit_message_reply_markup(reply_markup=None)
            await self._send_or_reply(f"⚙️ [SYSTEM] *Proposal {proposal_id} approved.* Executing...", target=query.message)
            if prop:
                async with TypingIndicator(query.message.chat if query.message else None):
                    res = await self._execute_proposal_command(prop)
                self.approvals.record_execution(proposal_id, res[:500])

                agent_resumed = False
                if self.agent and getattr(self.agent, "is_configured", False):
                    chat_id = str(query.message.chat_id) if query.message else "default"
                    if prop.proposal_type == "CONFIG_UPDATE":
                        continuation = (
                            f"Configuration update for '{prop.target}' was approved and applied.\n"
                            f"Execution output:\n{res}\n\n"
                            "Please confirm to the user that the configuration has been updated and the check engine reloaded."
                        )
                    else:
                        continuation = (
                            f"Action command '{prop.command}' on target '{prop.target}' was approved and executed.\n"
                            f"Execution output:\n{res}\n\n"
                            "Please confirm the result concisely to the user."
                        )
                    try:
                        async with TypingIndicator(query.message.chat if query.message else None):
                            followup = await self.agent.chat(continuation, chat_id=chat_id)
                        await self._send_or_reply(followup, target=query.message)
                        target_cid = query.message.chat_id if query.message else None
                        await self._dispatch_agent_proposals(chat_id=target_cid)
                        agent_resumed = True
                    except Exception as e:
                        logger.exception("Agent failed to synthesize proposal continuation: %s", e)

                if not agent_resumed:
                    await self._send_or_reply(res, target=query.message)

            if self.proposal_callback:
                await self.proposal_callback(proposal_id, True, user_id)

        elif data.startswith("deny:"):
            proposal_id = data.split(":", 1)[1]
            self.approvals.resolve_proposal(proposal_id, "DENY", user_id)
            prop = self.approvals.get_proposal(proposal_id)
            self.audit.log_event("PROPOSAL", f"tg:{user_id}", f"deny:{proposal_id}", "DENIED")
            await query.edit_message_reply_markup(reply_markup=None)
            cmd_name = prop.command if prop else f"Proposal {proposal_id}"
            await self._send_or_reply(f"⚙️ [SYSTEM] *Denied permission for `{cmd_name}`.*", target=query.message)
            if self.proposal_callback:
                await self.proposal_callback(proposal_id, False, user_id)

            if self.agent and getattr(self.agent, "is_configured", False) and prop:
                chat_id = str(query.message.chat_id) if query.message else "default"
                continuation = (
                    f"Permission to run command '{prop.command}' on target '{prop.target}' was denied by the user.\n"
                    "Conclude your previous inquiry without running or re-requesting this command."
                )
                try:
                    async with TypingIndicator(query.message.chat if query.message else None):
                        followup = await self.agent.chat(continuation, chat_id=chat_id)
                    await self._send_or_reply(followup, target=query.message)
                    target_cid = query.message.chat_id if query.message else None
                    await self._dispatch_agent_proposals(chat_id=target_cid)
                except Exception as e:
                    logger.exception("Agent failed to handle denial continuation: %s", e)

        elif data.startswith("mute:"):
            parts = data.split(":")
            check_id = parts[1]
            minutes = int(parts[2]) if len(parts) > 2 else 60
            if self.check_engine:
                until = self.check_engine.mute_check(check_id, minutes)
                until_str = until.strftime("%H:%M UTC") if until else f"{minutes}m"
                await query.edit_message_reply_markup(reply_markup=None)
                await self._send_or_reply(
                    f"🔔 [MONITOR] 🔕 *Muted check* `{check_id}` for {minutes}m (until `{until_str}`).",
                    target=query.message,
                )

        elif data.startswith("investigate:"):
            check_id = data.split(":", 1)[1]
            if self.agent and self.agent.is_configured:
                model_name = getattr(self.agent, "model_name", "LLM")
                chat_id = str(query.message.chat_id) if query.message else "default"
                self.audit.log_event("AI", f"tg:{user_id}", f"investigate:{check_id}", "TRIGGERED")
                await self._send_or_reply(
                    f"⚙️ [SYSTEM] *Wheatley is investigating `{check_id}` with {model_name}...*\n_Analyzing live telemetry and check state._",
                    target=query.message,
                )
                async with TypingIndicator(query.message.chat if query.message else None):
                    diagnosis = await self.agent.investigate_alert(
                        check_id=check_id,
                        issue_summary=f"Alert investigation requested for {check_id}",
                        chat_id=chat_id,
                    )
                await self._send_or_reply(
                    f"🤖 *Wheatley Diagnosis ({check_id}):*\n\n{diagnosis}",
                    target=query.message,
                )
                target_cid = query.message.chat_id if query.message else None
                await self._dispatch_agent_proposals(chat_id=target_cid)
            else:
                await self._send_or_reply("⚠️ LLM agent is not configured.", target=query.message)

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        text = update.message.text
        if not text or not text.strip():
            return

        self.audit.log_event("CHAT", f"tg:{update.effective_user.id}", "user_message", "RECEIVED", details={"text": text[:100]})

        try:
            if self.agent and self.agent.is_configured:
                chat_id = str(update.effective_chat.id) if update.effective_chat else "default"
                async with TypingIndicator(update.effective_chat):
                    response = await self.agent.chat(text, chat_id=chat_id)
                await self._send_or_reply(response, target=update.message)
                target_cid = update.effective_chat.id if update.effective_chat else None
                await self._dispatch_agent_proposals(chat_id=target_cid)
            else:
                await self._send_or_reply(
                    f"Received: \"{text}\"\n\n"
                    "_(Note: LLM is not configured. Configure `llm:` in `/etc/wheatley/config.yaml` and set credentials in `/etc/wheatley/secrets.env`.)_",
                    target=update.message,
                )
        except Exception as e:
            logger.exception("Error processing user message: %s", e)
            await self._send_or_reply(
                f"❌ *An error occurred while processing your message:* `{e}`",
                target=update.message,
            )

    async def start(self) -> None:
        logger.info("Initializing Telegram bot application...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram bot polling started successfully.")

    async def stop(self) -> None:
        logger.info("Stopping Telegram bot...")
        if self.app.updater and self.app.updater.running:
            await self.app.updater.stop()
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()

    async def send_message(self, text: str, parse_mode: Optional[str] = None) -> Any:
        if not self.primary_chat_id:
            logger.warning("Cannot send message: primary_chat_id is unset.")
            return None
        res = await self._send_or_reply(text, chat_id=self.primary_chat_id)
        return res[0] if res else None

    async def send_proposal(
        self,
        proposal_id: str,
        summary: str,
        command: str,
        target: str,
        risk: str,
        proposal_type: str = "ACTION",
        pattern: Optional[str] = None,
        chat_id: Optional[int] = None,
    ) -> Any:
        cid = chat_id or self.primary_chat_id
        if not cid:
            return None

        if proposal_type == "ALLOWLIST_RULE":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⚡ Run Once", callback_data=f"allow_once:{proposal_id}"),
                        InlineKeyboardButton(f"🛡 Always Allow on {target}", callback_data=f"allow_always:{proposal_id}"),
                    ],
                    [
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny:{proposal_id}"),
                    ],
                ]
            )
            msg = (
                f"⚙️ [SYSTEM] *COMMAND ALLOWLIST PROPOSAL*\n\n"
                f"*Target:* `{target}`\n"
                f"*Command to execute:*\n```bash\n{command}\n```\n"
                f"*Suggested Rule:* `{pattern or command}`\n"
                f"*Summary:* {summary}\n\n"
                f"Select an option to proceed:"
            )
        elif proposal_type == "CONFIG_UPDATE":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Approve Update", callback_data=f"approve:{proposal_id}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny:{proposal_id}"),
                    ]
                ]
            )
            diff_text = command
            try:
                import json
                data = json.loads(command)
                diff_text = data.get("diff", command)
            except Exception:
                pass
            msg = (
                f"⚙️ [SYSTEM] *CHECK CONFIG UPDATE PROPOSAL*\n\n"
                f"*File:* `{target}`\n"
                f"*Summary:* {summary}\n\n"
                f"*Proposed Diff:*\n```diff\n{diff_text}\n```\n"
                f"Tap Approve Update to apply changes and reload the check engine, or Deny to cancel."
            )
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{proposal_id}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny:{proposal_id}"),
                    ]
                ]
            )
            msg = (
                f"⚙️ [SYSTEM] *ACTION PROPOSAL REQUIRED*\n\n"
                f"*Target:* `{target}`\n"
                f"*Risk Level:* `{risk}`\n"
                f"*Summary:* {summary}\n\n"
                f"*Command to execute:*\n```bash\n{command}\n```\n\n"
                f"Tap Approve to execute, or Deny to reject."
            )

        res = await self._send_or_reply(msg, chat_id=cid, reply_markup=keyboard)
        return res[0] if res else None

    async def _dispatch_agent_proposals(self, chat_id: Optional[int] = None) -> None:
        """Deliver any deferred proposals directly after the agent's message."""
        if not self.agent or not hasattr(self.agent, "drain_pending_proposals"):
            return
        proposals = self.agent.drain_pending_proposals()
        for p in proposals:
            await self.send_proposal(
                proposal_id=p.id,
                summary=p.summary,
                command=p.command,
                target=p.target,
                risk=p.risk,
                proposal_type=p.proposal_type,
                pattern=p.pattern,
                chat_id=chat_id,
            )

    def set_command_handler(
        self, command: str, handler: Callable[..., Coroutine[Any, Any, None]]
    ) -> None:
        self._custom_commands[command] = handler
        self.app.add_handler(CommandHandler(command, handler))

    async def send_alert(self, text: str, extra: Optional[Dict[str, Any]] = None) -> Any:
        if not self.primary_chat_id:
            logger.warning("Cannot send alert: primary_chat_id is unset.")
            return None

        keyboard = None
        if extra and extra.get("type") == "alert" and extra.get("check_id"):
            check_id = extra["check_id"]
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔕 Mute 1h", callback_data=f"mute:{check_id}:60"),
                        InlineKeyboardButton("🔕 Mute 24h", callback_data=f"mute:{check_id}:1440"),
                    ],
                    [
                        InlineKeyboardButton("🔍 Investigate", callback_data=f"investigate:{check_id}"),
                    ],
                ]
            )

        res = await self._send_or_reply(text, chat_id=self.primary_chat_id, reply_markup=keyboard)
        return res[0] if res else None
