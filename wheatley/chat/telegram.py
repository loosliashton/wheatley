"""Telegram Bot Adapter for Wheatley"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple, Union
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
    split_telegram_message,
    strip_html_tags,
)
from wheatley.config import TelegramConfig
from wheatley.llm.agent import ChatTurnResult, TurnStatus
from wheatley.orchestrator import ProposalOrchestrator
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
        llm_agent: Optional[Any] = None,
        usage: Optional[Any] = None,
        shell: Optional[ShellExecutor] = None,
        approvals: Optional[ApprovalStore] = None,
        orchestrator: Optional[ProposalOrchestrator] = None,
    ):
        self.config = config
        self.audit = audit
        self.status_provider = status_provider
        self.discovery_provider = discovery_provider
        self.check_engine = check_engine
        self.agent = llm_agent
        self.usage = usage
        self.shell = shell or getattr(self.agent, "shell", None)
        self.approvals = (
            approvals
            or getattr(self.agent, "approvals", None)
            or ApprovalStore(getattr(audit, "db", getattr(audit, "db_path", "/var/lib/wheatley/wheatley.db")))
        )
        self.orchestrator = orchestrator or ProposalOrchestrator(
            approvals=self.approvals,
            audit=self.audit,
            shell=self.shell,
            check_engine=self.check_engine,
            agent=self.agent,
        )
        self.allowed_users: Set[int] = set(config.allowed_user_ids)
        self.primary_chat_id: Optional[int] = config.allowed_user_ids[0] if config.allowed_user_ids else None
        self.app: Application = ApplicationBuilder().token(config.bot_token).build()
        self.proposal_callback: Optional[Callable[[str, bool, int], Coroutine[Any, Any, None]]] = None
        self._custom_commands: Dict[str, Callable[..., Coroutine[Any, Any, None]]] = {}
        self._register_internal_handlers()

    @property
    def llm_agent(self) -> Optional[Any]:
        return self.agent

    @llm_agent.setter
    def llm_agent(self, value: Any) -> None:
        self.agent = value
        self.orchestrator.agent = value

    def health(self) -> Dict[str, Any]:
        """Return health status of the Telegram polling updater."""
        updater_running = bool(self.app.updater and self.app.updater.running)
        return {"ok": updater_running}

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
        self.app.add_handler(CommandHandler("update", self._cmd_update))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))

    async def _dispatch_send_chunk(
        self,
        html_chunk: str,
        target: Any = None,
        chat_id: Optional[int] = None,
        reply_markup: Optional[Any] = None,
    ) -> Optional[Any]:
        """Dispatch a single text chunk with HTML formatting, falling back to stripped plain text."""
        cid = chat_id or self.primary_chat_id
        if target is None and cid is None:
            logger.warning("Cannot send message: primary_chat_id is unset.")
            return None

        for payload, mode in ((html_chunk, ParseMode.HTML), (strip_html_tags(html_chunk), None)):
            try:
                if target is not None:
                    return await target.reply_text(
                        payload,
                        parse_mode=mode,
                        reply_markup=reply_markup,
                        disable_web_page_preview=True,
                    )
                return await self.app.bot.send_message(
                    chat_id=cid,
                    text=payload,
                    parse_mode=mode,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                if mode == ParseMode.HTML:
                    logger.warning("Telegram HTML send failed (%s), falling back to clean text", e)
                else:
                    logger.error("Failed clean plain text send fallback: %s", e)
        return None

    async def _dispatch_edit_chunk(
        self,
        message: Any,
        html_chunk: str,
        reply_markup: Optional[Any] = None,
    ) -> Tuple[bool, Optional[Any]]:
        """Attempt to edit message text with HTML; fallback to plain text or reply_markup update.

        Returns (success: bool, result: Optional[Any]).
        """
        for payload, mode in ((html_chunk, ParseMode.HTML), (strip_html_tags(html_chunk), None)):
            try:
                res = message.edit_text(
                    payload,
                    parse_mode=mode,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                if inspect.isawaitable(res):
                    res = await res
                return True, res or message
            except Exception as e:
                if "message is not modified" in str(e).lower():
                    if hasattr(message, "edit_reply_markup"):
                        try:
                            rm = message.edit_reply_markup(reply_markup=reply_markup)
                            if inspect.isawaitable(rm):
                                await rm
                        except Exception:
                            pass
                    return True, message
                if mode == ParseMode.HTML:
                    logger.debug("Telegram HTML edit failed (%s), attempting clean text fallback", e)
                else:
                    logger.warning("Telegram edit fallback failed (%s); sending new message", e)

        return False, None

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
        for idx, chunk in enumerate(chunks):
            chunk_markup = reply_markup if idx == len(chunks) - 1 else None
            html_chunk = markdown_to_telegram_html(chunk)
            res = await self._dispatch_send_chunk(
                html_chunk=html_chunk,
                target=target,
                chat_id=chat_id,
                reply_markup=chunk_markup,
            )
            if res is not None:
                results.append(res)
        return results

    async def _edit_or_send(
        self,
        text: str,
        message: Optional[Any] = None,
        chat_id: Optional[int] = None,
        reply_markup: Optional[Any] = None,
    ) -> List[Any]:
        """Edit an existing Telegram message in place if possible, falling back to sending a new message.

        If text exceeds Telegram's single-message limit (4096 chars), the first chunk
        is edited into the existing message and subsequent chunks are sent as follow-ups.
        """
        if not text:
            return []

        chunks = split_telegram_message(text)
        if not chunks:
            return []

        # If no message provided or message doesn't support edit_text, fall back to _send_or_reply
        if message is None or not hasattr(message, "edit_text"):
            return await self._send_or_reply(text, target=message, chat_id=chat_id, reply_markup=reply_markup)

        first_chunk = chunks[0]
        html_chunk = markdown_to_telegram_html(first_chunk)
        chunk_markup = reply_markup if len(chunks) == 1 else None

        edited, res = await self._dispatch_edit_chunk(message, html_chunk, reply_markup=chunk_markup)
        if not edited:
            return await self._send_or_reply(text, target=message, chat_id=chat_id, reply_markup=reply_markup)

        results: List[Any] = [res]
        if len(chunks) > 1:
            for idx in range(1, len(chunks)):
                sub_markup = reply_markup if idx == len(chunks) - 1 else None
                sub_res = await self._send_or_reply(
                    chunks[idx],
                    target=message,
                    chat_id=chat_id,
                    reply_markup=sub_markup,
                )
                results.extend(sub_res)

        return results

    def _build_continuation_keyboard(
        self,
        response: Any = None,
        hit_limit: Optional[bool] = None,
    ) -> Optional[InlineKeyboardMarkup]:
        """Build inline keyboard with a Continue button when the agent turn hit the iteration cap."""
        if hit_limit is None:
            hit_limit = getattr(response, "hit_limit", None)
        if hit_limit is None and getattr(response, "status", None) is not None:
            hit_limit = response.status == TurnStatus.HIT_BUDGET
        if hit_limit is None:
            hit_limit = bool(self.agent and getattr(self.agent, "last_turn_hit_limit", False) is True)
            if not hit_limit and isinstance(response, str):
                hit_limit = (
                    "Diagnostic tool budget reached" in response
                    or "Reached maximum function calling iterations" in response
                )
        if not hit_limit:
            return None
        iterations = 10
        if self.agent and hasattr(self.agent, "config"):
            val = getattr(getattr(self.agent.config, "llm", None), "max_tool_iterations", 10)
            if isinstance(val, int):
                iterations = val
        keyboard = [
            [
                InlineKeyboardButton(
                    f"▶️ Continue Investigating (+{iterations})",
                    callback_data="agent_continue",
                ),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

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
            "/update - Check for updates and pull latest code from GitHub\n"
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
        placeholders = await self._send_or_reply(
            "⚙️ [SYSTEM] *Scanning Proxmox nodes and probing guests for services...*\n_This may take a few seconds._",
            target=update.message,
        )
        placeholder = placeholders[0] if placeholders else update.message
        if self.discovery_provider:
            try:
                async with TypingIndicator(update.effective_chat):
                    summary = await self.discovery_provider()
                summary_clean = summary.lstrip("🔍 ")
                await self._edit_or_send(f"⚙️ [SYSTEM] {summary_clean}", message=placeholder)
            except Exception as e:
                logger.exception("Telegram /discover failed")
                await self._edit_or_send(f"⚙️ [SYSTEM] *Discovery failed:* `{e}`", message=placeholder)
        else:
            await self._edit_or_send("⚙️ [SYSTEM] Auto-discovery provider not connected to bot.", message=placeholder)

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
        global_sched = getattr(self.check_engine.config, "schedule", [])
        if global_sched:
            sched_strs = [f"`{w.format_short()}`" for w in global_sched]
            lines.append(f"🗓️ *Global Pause Windows:* {', '.join(sched_strs)}\n")

        for s in states:
            cid = s["id"]
            ctype = s["type"]
            status = s["status"]
            is_muted = s["is_muted"]
            muted_until = s["muted_until"]
            is_sched_pause = s.get("is_scheduled_pause", False)
            active_win = s.get("active_schedule_window")

            if is_muted:
                until_str = muted_until.strftime("%H:%M UTC") if muted_until else "muted"
                icon = "🔕"
                status_str = f"MUTED (until {until_str})"
            elif is_sched_pause and active_win:
                icon = "⏸️"
                if isinstance(active_win, dict):
                    win_label = f"{active_win.get('start', '')}-{active_win.get('end', '')}"
                    if active_win.get("reason"):
                        win_label += f": {active_win['reason']}"
                else:
                    win_label = str(active_win)
                status_str = f"PAUSED ({win_label})"
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

            chk_sched = s.get("schedule") or []
            sched_suffix = ""
            if chk_sched and not is_sched_pause:
                win_list = ", ".join(f"{w['start']}-{w['end']}" for w in chk_sched)
                sched_suffix = f" _(pause: {win_list})_"

            lines.append(f"{icon} `{cid}` ({ctype}): *{status_str}*{sched_suffix}")

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
                "Example: `/mute web-app 120` (default: 60 minutes)\n\n"
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
                await self._send_or_reply("⚙️ [SYSTEM] Minutes must be a whole number, e.g. `/mute web-app 60`", target=update.message)
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
        tracker = (self.agent.usage if self.agent and hasattr(self.agent, "usage") else None) or self.usage
        if tracker:
            msg = tracker.format_usage_markdown(
                provider=getattr(self.agent, "provider_name", "llm") if self.agent else "llm",
                model=getattr(self.agent, "model_name", "default") if self.agent else "default",
            )
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

    async def _run_investigation(
        self,
        check_id: str,
        target_message: Any,
        chat: Optional[Any] = None,
        chat_id_str: Optional[str] = None,
        issue_summary: Optional[str] = None,
        initial_notice: Optional[str] = None,
    ) -> None:
        if not self.agent or not self.agent.is_configured:
            await self._send_or_reply("⚙️ [SYSTEM] LLM is not configured.", target=target_message)
            return

        model_name = getattr(self.agent, "model_name", "LLM")
        cid_str = chat_id_str or (str(chat.id) if chat else "default")
        notice = initial_notice or f"⚙️ [SYSTEM] *Investigating `{check_id}` with {model_name}...*"
        placeholders = await self._send_or_reply(notice, target=target_message)
        placeholder = placeholders[0] if placeholders else target_message

        async with TypingIndicator(chat):
            diagnosis = await self.agent.investigate_alert(
                check_id=check_id,
                issue_summary=issue_summary or f"Manual investigation triggered for {check_id}",
                chat_id=cid_str,
            )
        target_cid = chat.id if chat else None
        if getattr(diagnosis, "status", None) == TurnStatus.HALTED_ON_APPROVAL:
            await self._edit_or_send(
                f"🤖 *Wheatley Diagnosis ({check_id}):*\n\n_Diagnostic command requires authorization below._",
                message=placeholder,
            )
            await self._dispatch_agent_proposals(turn=diagnosis, chat_id=target_cid)
            return

        keyboard = self._build_continuation_keyboard(diagnosis)
        text = getattr(diagnosis, "text", str(diagnosis))
        await self._edit_or_send(
            f"🤖 *Wheatley Diagnosis ({check_id}):*\n\n{text}",
            message=placeholder,
            reply_markup=keyboard,
        )
        await self._dispatch_agent_proposals(turn=diagnosis, chat_id=target_cid)

    async def _cmd_investigate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        args = context.args or []
        if not args:
            await self._send_or_reply("⚙️ [SYSTEM] *Usage:* `/investigate <check_id>` (e.g. `/investigate web-app`)", target=update.message)
            return

        check_id = args[0]
        await self._run_investigation(
            check_id=check_id,
            target_message=update.message,
            chat=update.effective_chat,
            issue_summary=f"Manual investigation triggered for {check_id}",
        )

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

    async def send_update_proposal(
        self,
        check_res: Any,
        chat_id: Optional[int] = None,
        expires_minutes: int = 1440,
    ) -> Optional[Any]:
        """Create a SELF_UPDATE proposal and send it via Telegram with an inline update button."""
        commits = getattr(check_res, "incoming_commits", []) or []
        if commits:
            commits_summary = "\n".join(f"• `{c}`" for c in commits[:8])
            if len(commits) > 8:
                commits_summary += f"\n• _... and {len(commits) - 8} more_"
        else:
            commits_summary = "• _(latest upstream commits)_"

        summary = f"Update Wheatley to {check_res.remote_commit} ({len(commits)} new commits)"
        details = (
            f"• Current: `{check_res.current_commit}`\n"
            f"• Latest: `{check_res.remote_commit}` on `{check_res.branch}`\n\n"
            f"*Incoming Commits:*\n{commits_summary}"
        )
        prop = self.approvals.create_proposal(
            proposal_type="SELF_UPDATE",
            target="localhost",
            command="wheatley update",
            summary=summary,
            risk="MEDIUM",
            justification=details,
            expires_minutes=expires_minutes,
        )
        return await self.send_proposal(
            proposal_id=prop.id,
            summary=prop.summary,
            command=prop.command,
            target=prop.target,
            risk=prop.risk,
            proposal_type=prop.proposal_type,
            justification=details,
            chat_id=chat_id,
        )

    async def _cmd_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        from wheatley.updater import WheatleyUpdater

        async with TypingIndicator(update.effective_chat):
            updater = WheatleyUpdater()
            check_res = await asyncio.to_thread(updater.check_for_updates)
            if check_res.error:
                await self._send_or_reply(
                    f"⚙️ [SYSTEM] *Update Check Failed:*\n`{check_res.error}`",
                    target=update.message,
                )
                return

            if not check_res.has_update:
                await self._send_or_reply(
                    f"⚙️ [SYSTEM] *Wheatley is up to date!* (commit `{check_res.current_commit}`)\n"
                    f"_{check_res.current_message}_",
                    target=update.message,
                )
                return

            await self.send_update_proposal(
                check_res=check_res,
                chat_id=update.effective_chat.id if update.effective_chat else None,
            )

    def _sync_orchestrator(self) -> None:
        self.orchestrator.approvals = self.approvals
        self.orchestrator.audit = self.audit
        self.orchestrator.shell = self.shell
        self.orchestrator.check_engine = self.check_engine
        self.orchestrator.agent = self.agent

    async def _schedule_service_restart(self, delay_seconds: float = 2.0) -> None:
        self._sync_orchestrator()
        await self.orchestrator.schedule_service_restart(delay_seconds=delay_seconds)

    async def _execute_proposal_command(self, prop: Any) -> str:
        self._sync_orchestrator()
        return await self.orchestrator.execute_proposal_command(prop)

    async def _resume_agent_after_proposal(
        self,
        query: Any,
        prop: Any,
        decision: str,
        execution_output: Optional[str] = None,
    ) -> bool:
        """Resume the LLM agent with a continuation prompt if configured."""
        self._sync_orchestrator()
        if not (self.agent and getattr(self.agent, "is_configured", False) and prop):
            return False

        chat_id = str(query.message.chat_id) if query.message else "default"
        continuation = self.orchestrator.build_continuation_prompt(prop, decision, execution_output)
        try:
            async with TypingIndicator(query.message.chat if query.message else None):
                followup = await self.agent.chat(continuation, chat_id=chat_id)
            target_cid = query.message.chat_id if query.message else None
            await self._deliver_agent_turn(followup, target_message=query.message, chat_id=target_cid)
            return True
        except Exception as e:
            logger.exception("Agent failed to synthesize proposal continuation: %s", e)
            return False

    @staticmethod
    async def _clear_reply_markup(query: Any) -> None:
        """Safely strip inline keyboard buttons from a callback query message."""
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    async def _update_proposal_card(
        self,
        message: Any,
        prop: Any,
        status_line: str,
        reply_markup: Optional[Any] = None,
    ) -> List[Any]:
        """Format and update a proposal card in place with a new status footer."""
        if not prop:
            return []
        card = self._format_proposal_content(
            proposal_type=prop.proposal_type,
            target=prop.target,
            command=prop.command,
            summary=prop.summary,
            risk=prop.risk,
            pattern=prop.pattern,
            justification=getattr(prop, "justification", None),
            status_line=status_line,
        )
        return await self._edit_or_send(card, message=message, reply_markup=reply_markup)

    async def _execute_and_resolve_proposal(
        self,
        query: Any,
        prop: Any,
        decision: str,
        exec_status: str,
        done_status: str,
    ) -> None:
        """Shared execution lifecycle: updates card to executing, executes command, records result, updates card to done, and resumes agent."""
        if prop:
            await self._update_proposal_card(query.message, prop, exec_status)
        else:
            await self._clear_reply_markup(query)

        async with TypingIndicator(query.message.chat if query.message else None):
            res = await self._execute_proposal_command(prop)
        if prop:
            self.approvals.record_execution(prop.id, res[:500])
            await self._update_proposal_card(query.message, prop, done_status)

        agent_resumed = await self._resume_agent_after_proposal(query, prop, decision, res)
        if not agent_resumed:
            await self._send_or_reply(res, target=query.message)

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
                await self._clear_reply_markup(query)
                await self._send_or_reply(f"⚙️ [SYSTEM] *Could not execute:* {msg}", target=query.message)
                return

            prop = self.approvals.get_proposal(proposal_id)
            self.audit.log_event("ALLOWLIST", f"tg:{user_id}", f"allow_once:{proposal_id}", "APPROVED", details={"cmd": prop.command if prop else ""})
            target_str = prop.target if prop else "target"

            await self._execute_and_resolve_proposal(
                query=query,
                prop=prop,
                decision="ALLOW_ONCE",
                exec_status=f"⏳ *Status:* Executing once on `{target_str}`...",
                done_status=f"⚡ *Status:* Approved (Run Once) — Executed on `{target_str}`",
            )

        elif data.startswith("allow_always:"):
            proposal_id = data.split(":", 1)[1]
            success, msg = self.approvals.resolve_proposal(proposal_id, "ALLOW_ALWAYS", user_id)
            if not success:
                await self._clear_reply_markup(query)
                await self._send_or_reply(f"⚙️ [SYSTEM] *Could not save rule:* {msg}", target=query.message)
                return

            prop = self.approvals.get_proposal(proposal_id)
            self.audit.log_event("ALLOWLIST", f"tg:{user_id}", f"allow_always:{proposal_id}", "ADDED", details={"pattern": prop.pattern if prop else "", "target": prop.target if prop else ""})
            pattern_str = prop.pattern if prop else "rule"

            await self._execute_and_resolve_proposal(
                query=query,
                prop=prop,
                decision="ALLOW_ALWAYS",
                exec_status=f"⏳ *Status:* Adding rule `{pattern_str}` & executing...",
                done_status=f"🛡 *Status:* Always Allowed — Added rule `{pattern_str}` & executed",
            )

        elif data.startswith("approve:"):
            proposal_id = data.split(":", 1)[1]
            success, msg = self.approvals.resolve_proposal(proposal_id, "APPROVE", user_id)
            if not success:
                await self._clear_reply_markup(query)
                await self._send_or_reply(f"❌ *Approval failed:* {msg}", target=query.message)
                return

            prop = self.approvals.get_proposal(proposal_id)
            self.audit.log_event("PROPOSAL", f"tg:{user_id}", f"approve:{proposal_id}", "APPROVED")

            if prop:
                if prop.proposal_type == "CONFIG_UPDATE":
                    final_status = "✅ *Status:* Approved — Config update applied & checks reloaded"
                elif prop.proposal_type == "SELF_UPDATE":
                    final_status = "🚀 *Status:* Approved — Update triggered"
                else:
                    final_status = "✅ *Status:* Approved & Executed"

                await self._execute_and_resolve_proposal(
                    query=query,
                    prop=prop,
                    decision="APPROVE",
                    exec_status="⏳ *Status:* Approved — Executing...",
                    done_status=final_status,
                )
            else:
                await self._clear_reply_markup(query)

            if self.proposal_callback:
                await self.proposal_callback(proposal_id, True, user_id)

        elif data.startswith("deny:"):
            proposal_id = data.split(":", 1)[1]
            self.approvals.resolve_proposal(proposal_id, "DENY", user_id)
            prop = self.approvals.get_proposal(proposal_id)
            self.audit.log_event("PROPOSAL", f"tg:{user_id}", f"deny:{proposal_id}", "DENIED")

            if prop:
                await self._update_proposal_card(query.message, prop, "❌ *Status:* Denied by user")
            else:
                await self._clear_reply_markup(query)

            if self.proposal_callback:
                await self.proposal_callback(proposal_id, False, user_id)

            await self._resume_agent_after_proposal(query, prop, "DENY")

        elif data.startswith("mute:"):
            parts = data.split(":")
            check_id = parts[1]
            minutes = int(parts[2]) if len(parts) > 2 else 60
            if self.check_engine:
                until = self.check_engine.mute_check(check_id, minutes)
                until_str = until.strftime("%H:%M UTC") if until else f"{minutes}m"
                alert_text = getattr(query.message, "text", "") or getattr(query.message, "caption", "") or ""
                base_text = alert_text.split("\n\n🔕 *Muted")[0]
                updated_text = f"{base_text}\n\n🔕 *Muted for {minutes}m (until {until_str})*"
                keep_keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔍 Investigate", callback_data=f"investigate:{check_id}")]]
                )
                await self._edit_or_send(
                    updated_text,
                    message=query.message,
                    reply_markup=keep_keyboard,
                )

        elif data.startswith("investigate:"):
            check_id = data.split(":", 1)[1]
            self.audit.log_event("AI", f"tg:{user_id}", f"investigate:{check_id}", "TRIGGERED")
            model_name = getattr(self.agent, "model_name", "LLM") if self.agent else "LLM"
            await self._run_investigation(
                check_id=check_id,
                target_message=query.message,
                chat=query.message.chat if query.message else None,
                chat_id_str=str(query.message.chat_id) if query.message else "default",
                issue_summary=f"Alert investigation requested for {check_id}",
                initial_notice=f"⚙️ [SYSTEM] *Wheatley is investigating `{check_id}` with {model_name}...*\n_Analyzing live telemetry and check state._",
            )

        elif data.startswith("agent_continue"):
            await self._clear_reply_markup(query)
            if not (self.agent and getattr(self.agent, "is_configured", False)):
                await self._send_or_reply("⚠️ LLM agent is not configured.", target=query.message)
                return

            chat_target = query.message.chat if query.message else None
            chat_id_str = str(query.message.chat_id) if query.message else "default"
            async with TypingIndicator(chat_target):
                followup = await self.agent.chat(
                    "Continue the investigation from where you left off.",
                    chat_id=chat_id_str,
                )
            target_cid = query.message.chat_id if query.message else None
            await self._deliver_agent_turn(
                turn=followup,
                target_message=query.message,
                chat_id=target_cid,
            )

    async def _deliver_agent_turn(
        self,
        turn: Union[ChatTurnResult, str],
        target_message: Any = None,
        chat_id: Optional[int] = None,
    ) -> None:
        """Deliver agent response and proposals, omitting redundant allowlist boilerplate text in chat."""
        if getattr(turn, "status", None) == TurnStatus.HALTED_ON_APPROVAL:
            proposals = await self._dispatch_agent_proposals(
                turn=turn, chat_id=chat_id, target_message=target_message
            )
            if proposals:
                return

        keyboard = self._build_continuation_keyboard(turn)
        text = getattr(turn, "text", str(turn))
        await self._send_or_reply(
            text, target=target_message, chat_id=chat_id, reply_markup=keyboard
        )
        if getattr(turn, "status", None) != TurnStatus.HALTED_ON_APPROVAL:
            await self._dispatch_agent_proposals(turn=turn, chat_id=chat_id)

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
                target_cid = update.effective_chat.id if update.effective_chat else None
                await self._deliver_agent_turn(
                    turn=response,
                    target_message=update.message,
                    chat_id=target_cid,
                )
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

    @staticmethod
    def _format_proposal_content(
        proposal_type: str,
        target: str,
        command: str,
        summary: str,
        risk: str,
        pattern: Optional[str] = None,
        justification: Optional[str] = None,
        status_line: Optional[str] = None,
    ) -> str:
        if proposal_type == "ALLOWLIST_RULE":
            justification_line = f"*Justification:* {justification}\n" if justification else ""
            footer = status_line or "Select an option to proceed:"
            return (
                f"⚙️ [SYSTEM] *COMMAND ALLOWLIST PROPOSAL*\n\n"
                f"*Target:* `{target}`\n"
                f"*Command to execute:*\n```bash\n{command}\n```\n"
                f"*Suggested Rule:* `{pattern or command}`\n"
                f"*Summary:* {summary}\n"
                f"{justification_line}\n"
                f"{footer}"
            )
        elif proposal_type == "CONFIG_UPDATE":
            diff_text = command
            try:
                data = json.loads(command)
                diff_text = data.get("diff", command)
            except Exception:
                pass
            footer = status_line or "Tap Approve Update to apply changes and reload the check engine, or Deny to cancel."
            return (
                f"⚙️ [SYSTEM] *CHECK CONFIG UPDATE PROPOSAL*\n\n"
                f"*File:* `{target}`\n"
                f"*Summary:* {summary}\n\n"
                f"*Proposed Diff:*\n```diff\n{diff_text}\n```\n"
                f"{footer}"
            )
        elif proposal_type == "SELF_UPDATE":
            commits_block = f"\n\n*New Commits:*\n{justification}\n" if justification else "\n\n*Action:* Pull latest commits, update packages, and reload Wheatley\n"
            footer = status_line or "Tap Update Now to proceed or Cancel."
            return (
                f"⚙️ [SYSTEM] *WHEATLEY UPDATE PROPOSAL*\n\n"
                f"*Summary:* {summary}\n"
                f"{commits_block}\n"
                f"{footer}"
            )
        else:
            footer = status_line or "Tap Approve to execute, or Deny to reject."
            return (
                f"⚙️ [SYSTEM] *ACTION PROPOSAL REQUIRED*\n\n"
                f"*Target:* `{target}`\n"
                f"*Risk Level:* `{risk}`\n"
                f"*Summary:* {summary}\n\n"
                f"*Command to execute:*\n```bash\n{command}\n```\n\n"
                f"{footer}"
            )

    async def send_proposal(
        self,
        proposal_id: str,
        summary: str,
        command: str,
        target: str,
        risk: str,
        proposal_type: str = "ACTION",
        pattern: Optional[str] = None,
        justification: Optional[str] = None,
        chat_id: Optional[int] = None,
        target_message: Optional[Any] = None,
    ) -> Any:
        cid = chat_id or self.primary_chat_id
        if not cid and not target_message:
            return None

        if proposal_type == "ALLOWLIST_RULE":
            from wheatley.tools.allowlist import validate_proposed_pattern

            can_always_allow = bool(pattern and validate_proposed_pattern(pattern)[0])
            row1 = [InlineKeyboardButton("⚡ Run Once", callback_data=f"allow_once:{proposal_id}")]
            if can_always_allow:
                row1.append(
                    InlineKeyboardButton(f"🛡 Always Allow on {target}", callback_data=f"allow_always:{proposal_id}")
                )
            keyboard = InlineKeyboardMarkup(
                [
                    row1,
                    [
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny:{proposal_id}"),
                    ],
                ]
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
        elif proposal_type == "SELF_UPDATE":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🚀 Update Now", callback_data=f"approve:{proposal_id}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"deny:{proposal_id}"),
                    ]
                ]
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

        msg = self._format_proposal_content(
            proposal_type=proposal_type,
            target=target,
            command=command,
            summary=summary,
            risk=risk,
            pattern=pattern,
            justification=justification,
        )
        res = await self._send_or_reply(msg, target=target_message, chat_id=cid, reply_markup=keyboard)
        return res[0] if res else None

    async def _dispatch_agent_proposals(
        self,
        turn: Any = None,
        chat_id: Optional[int] = None,
        target_message: Optional[Any] = None,
    ) -> List[Any]:
        """Deliver proposals attached to the agent turn (or drained from pending_proposals)."""
        proposals = getattr(turn, "proposals", None)
        if proposals is not None:
            if self.agent and hasattr(self.agent, "drain_pending_proposals"):
                self.agent.drain_pending_proposals()
        elif self.agent and hasattr(self.agent, "drain_pending_proposals"):
            proposals = self.agent.drain_pending_proposals()
        else:
            proposals = []

        for p in proposals:
            await self.send_proposal(
                proposal_id=p.id,
                summary=p.summary,
                command=p.command,
                target=p.target,
                risk=p.risk,
                proposal_type=p.proposal_type,
                pattern=p.pattern,
                justification=getattr(p, "justification", None),
                chat_id=chat_id,
                target_message=target_message,
            )
        return proposals

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
