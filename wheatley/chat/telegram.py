"""Telegram Bot Adapter for Wheatley"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Dict, Optional, Set
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

from wheatley.audit import AuditLogger
from wheatley.chat.base import ChatInterface
from wheatley.config import TelegramConfig

logger = logging.getLogger("wheatley.chat.telegram")


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
    ):
        self.config = config
        self.audit = audit
        self.status_provider = status_provider
        self.discovery_provider = discovery_provider
        self.check_engine = check_engine
        self.agent = llm_agent or gemini_agent
        self.budget = budget
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
        self.app.add_handler(CommandHandler("discover", self._cmd_discover))
        self.app.add_handler(CommandHandler("audit", self._cmd_audit))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_name = update.effective_user.first_name if update.effective_user else "Commander"
        msg = (
            f"👋 *Greetings, {user_name}!*\n\n"
            f"I'm *Wheatley*, your homelab monitoring & AI administration agent.\n\n"
            f"• `/status` - Check node and service health\n"
            f"• `/checks` - View all active checks & mute states\n"
            f"• `/usage` - View LLM token spend & activity\n"
            f"• `/discover` - Auto-detect services & containers\n"
            f"• `/audit` - Inspect recent action logs\n"
            f"• `/help` - See all available commands\n\n"
            f"You can also chat directly with me about any homelab questions!"
        )
        self.audit.log_event("CMD", f"tg:{update.effective_user.id}", "/start", "SUCCESS")
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        help_text = (
            "🛠 *Wheatley Commands*\n\n"
            "/status - Cluster & guest health overview\n"
            "/checks - View all health checks and mute status\n"
            "/usage - LLM token consumption & activity\n"
            "/investigate <id> - Run AI diagnosis on a service\n"
            "/mute <id> [mins] - Silence alerts for a check\n"
            "/unmute <id> - Re-enable alerts for a check\n"
            "/discover - Probe nodes & guests to discover active services\n"
            "/audit - Recent security and activity audit log\n"
            "/help - Display this manual\n\n"
            "💬 *AI Chat:* Simply send a message to ask questions or diagnose services!"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self.status_provider:
            status_text = await self.status_provider()
        else:
            status_text = "🟢 *Wheatley Core:* Online\nNode checks initializing..."
        await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_discover(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        self.audit.log_event("CMD", f"tg:{update.effective_user.id}", "/discover", "SUCCESS")
        await update.message.reply_text(
            "🔍 *Scanning Proxmox nodes and probing guests for services...*\n_This may take a few seconds._",
            parse_mode=ParseMode.MARKDOWN,
        )
        if self.discovery_provider:
            try:
                summary = await self.discovery_provider()
                await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.exception("Telegram /discover failed")
                await update.message.reply_text(f"❌ *Discovery failed:* `{e}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("⚠️ Auto-discovery provider not connected to bot.")

    async def _cmd_audit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        logs = self.audit.get_recent_logs(limit=8)
        if not logs:
            await update.message.reply_text("📋 Audit log is currently empty.")
            return

        lines = ["📋 *Recent Audit Events:*"]
        for entry in logs:
            ts = entry["timestamp"][:19].replace("T", " ")
            lines.append(
                f"• `[{ts}]` *{entry['action']}* ({entry['status']}) - `{entry['source']}`"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _cmd_checks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.check_engine:
            await update.message.reply_text("⚠️ Check engine is not connected to bot.")
            return

        states = self.check_engine.get_check_states()
        if not states:
            await update.message.reply_text("ℹ️ No health checks currently configured.")
            return

        lines = [f"🔍 *Wheatley Monitored Checks ({len(states)})*\n"]
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
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.check_engine:
            await update.message.reply_text("⚠️ Check engine is not connected.")
            return

        args = context.args or []
        if not args:
            states = self.check_engine.get_check_states()
            check_ids = [f"`{s['id']}`" for s in states]
            msg = (
                "⚠️ *Usage:* `/mute <check_id> [minutes]`\n"
                "Example: `/mute plex 120` (default: 60 minutes)\n\n"
                f"Available checks: {', '.join(check_ids) if check_ids else 'None'}"
            )
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return

        check_id = args[0]
        minutes = 60
        if len(args) > 1:
            try:
                minutes = int(args[1])
            except ValueError:
                await update.message.reply_text("❌ Minutes must be a whole number, e.g. `/mute plex 60`")
                return

        until = self.check_engine.mute_check(check_id, minutes)
        if not until:
            await update.message.reply_text(f"❌ Check `{check_id}` not found.", parse_mode=ParseMode.MARKDOWN)
            return

        until_str = until.strftime("%Y-%m-%d %H:%M:%S UTC")
        await update.message.reply_text(
            f"🔕 *Muted check* `{check_id}` for {minutes} minutes.\nAlerts silenced until `{until_str}`.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.check_engine:
            await update.message.reply_text("⚠️ Check engine is not connected.")
            return

        args = context.args or []
        if not args:
            await update.message.reply_text("⚠️ *Usage:* `/unmute <check_id>`", parse_mode=ParseMode.MARKDOWN)
            return

        check_id = args[0]
        success = self.check_engine.unmute_check(check_id)
        if success:
            await update.message.reply_text(f"🔔 *Unmuted check* `{check_id}`. Alerts are re-enabled.", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"❌ Check `{check_id}` not found.", parse_mode=ParseMode.MARKDOWN)

    async def _cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self.agent and hasattr(self.agent, "usage"):
            msg = self.agent.usage.format_usage_markdown(
                provider=getattr(self.agent, "provider_name", "llm"),
                model=getattr(self.agent, "model_name", "default"),
            )
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        elif self.budget:
            msg = self.budget.format_usage_markdown()
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("⚠️ Token usage tracker is not connected.")

    async def _cmd_investigate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("⚠️ *Usage:* `/investigate <check_id>` (e.g. `/investigate plex`)", parse_mode=ParseMode.MARKDOWN)
            return

        check_id = args[0]
        if not self.agent or not self.agent.is_configured:
            await update.message.reply_text("⚠️ LLM is not configured.")
            return

        model_name = getattr(self.agent, "model_name", "LLM")
        await update.message.reply_text(
            f"🔍 *Investigating `{check_id}` with {model_name}...*",
            parse_mode=ParseMode.MARKDOWN,
        )
        diagnosis = await self.agent.investigate_alert(check_id, f"Manual investigation triggered for {check_id}")
        try:
            await update.message.reply_text(f"🤖 *Wheatley Diagnosis ({check_id}):*\n\n{diagnosis}", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(f"🤖 *Wheatley Diagnosis ({check_id}):*\n\n{diagnosis}", parse_mode=None)

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not self._is_authorized(update):
            return
        await query.answer()

        data = query.data or ""
        user_id = update.effective_user.id

        if data.startswith("approve:"):
            proposal_id = data.split(":", 1)[1]
            self.audit.log_event("PROPOSAL", f"tg:{user_id}", f"approve:{proposal_id}", "APPROVED")
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"✅ *Proposal {proposal_id} approved.* Executing...")
            if self.proposal_callback:
                await self.proposal_callback(proposal_id, True, user_id)

        elif data.startswith("deny:"):
            proposal_id = data.split(":", 1)[1]
            self.audit.log_event("PROPOSAL", f"tg:{user_id}", f"deny:{proposal_id}", "DENIED")
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"❌ *Proposal {proposal_id} denied.*")
            if self.proposal_callback:
                await self.proposal_callback(proposal_id, False, user_id)

        elif data.startswith("mute:"):
            parts = data.split(":")
            check_id = parts[1]
            minutes = int(parts[2]) if len(parts) > 2 else 60
            if self.check_engine:
                until = self.check_engine.mute_check(check_id, minutes)
                until_str = until.strftime("%H:%M UTC") if until else f"{minutes}m"
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(
                    f"🔕 *Muted check* `{check_id}` for {minutes}m (until `{until_str}`).",
                    parse_mode=ParseMode.MARKDOWN,
                )

        elif data.startswith("investigate:"):
            check_id = data.split(":", 1)[1]
            if self.agent and self.agent.is_configured:
                model_name = getattr(self.agent, "model_name", "LLM")
                self.audit.log_event("AI", f"tg:{user_id}", f"investigate:{check_id}", "TRIGGERED")
                await query.message.reply_text(
                    f"🔍 *Wheatley is investigating `{check_id}` with {model_name}...*\n_Analyzing live telemetry and check state._",
                    parse_mode=ParseMode.MARKDOWN,
                )
                diagnosis = await self.agent.investigate_alert(
                    check_id=check_id,
                    issue_summary=f"Alert investigation requested for {check_id}",
                )
                try:
                    await query.message.reply_text(
                        f"🤖 *Wheatley Diagnosis ({check_id}):*\n\n{diagnosis}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    await query.message.reply_text(
                        f"🤖 *Wheatley Diagnosis ({check_id}):*\n\n{diagnosis}",
                        parse_mode=None,
                    )
            else:
                await query.message.reply_text("⚠️ LLM agent is not configured.")

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        text = update.message.text
        if not text or not text.strip():
            return

        self.audit.log_event("CHAT", f"tg:{update.effective_user.id}", "user_message", "RECEIVED", details={"text": text[:100]})

        if self.agent and self.agent.is_configured:
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass

            response = await self.agent.chat(text)
            try:
                await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.warning("Markdown reply failed (%s), sending plain text", e)
                await update.message.reply_text(response, parse_mode=None)
        else:
            await update.message.reply_text(
                f"Received: \"{text}\"\n\n"
                "_(Note: LLM is not configured. Configure `llm:` in `/etc/wheatley/config.yaml` and set credentials in `/etc/wheatley/secrets.env`.)_",
                parse_mode=ParseMode.MARKDOWN,
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

    async def send_message(self, text: str, parse_mode: Optional[str] = "Markdown") -> Any:
        if not self.primary_chat_id:
            logger.warning("Cannot send message: primary_chat_id is unset.")
            return None
        pm = ParseMode.MARKDOWN if parse_mode == "Markdown" else None
        try:
            return await self.app.bot.send_message(
                chat_id=self.primary_chat_id, text=text, parse_mode=pm
            )
        except Exception as e:
            # Fallback to plain text if markdown formatting failed
            logger.warning("Markdown send failed (%s), falling back to raw text", e)
            return await self.app.bot.send_message(
                chat_id=self.primary_chat_id, text=text, parse_mode=None
            )

    async def send_proposal(
        self,
        proposal_id: str,
        summary: str,
        command: str,
        target: str,
        risk: str,
    ) -> Any:
        if not self.primary_chat_id:
            return None

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve:{proposal_id}"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"deny:{proposal_id}"),
                ]
            ]
        )

        msg = (
            f"⚠️ *ACTION PROPOSAL REQUIRED*\n\n"
            f"*Target:* `{target}`\n"
            f"*Risk Level:* `{risk}`\n"
            f"*Summary:* {summary}\n\n"
            f"*Command to execute:*\n```bash\n{command}\n```\n\n"
            f"Tap Approve to execute on host, or Deny to reject."
        )

        return await self.app.bot.send_message(
            chat_id=self.primary_chat_id,
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
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

        try:
            return await self.app.bot.send_message(
                chat_id=self.primary_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.warning("Markdown alert send failed (%s), falling back to raw text", e)
            return await self.app.bot.send_message(
                chat_id=self.primary_chat_id,
                text=text,
                parse_mode=None,
                reply_markup=keyboard,
            )

