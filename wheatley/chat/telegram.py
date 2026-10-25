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
    ):
        self.config = config
        self.audit = audit
        self.status_provider = status_provider
        self.allowed_users: Set[int] = set(config.allowed_user_ids)
        self.primary_chat_id: Optional[int] = config.allowed_user_ids[0] if config.allowed_user_ids else None
        self.app: Application = ApplicationBuilder().token(config.bot_token).build()
        self.proposal_callback: Optional[Callable[[str, bool, int], Coroutine[Any, Any, None]]] = None
        self._custom_commands: Dict[str, Callable[..., Coroutine[Any, Any, None]]] = {}
        self._register_internal_handlers()

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
            f"• Use /status to check node and service health\n"
            f"• Use /audit to inspect recent action logs\n"
            f"• Use /help to see all available commands\n\n"
            f"All state changes require your explicit button approval."
        )
        self.audit.log_event("CMD", f"tg:{update.effective_user.id}", "/start", "SUCCESS")
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        help_text = (
            "🛠 *Wheatley Commands*\n\n"
            "/status - Cluster & guest health overview\n"
            "/audit - Recent security and activity audit log\n"
            "/usage - GCP credit and token consumption\n"
            "/mute <check_id> <mins> - Temporarily silence alerts\n"
            "/help - Display this manual\n"
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

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        text = update.message.text
        # Fallback response for unrouted user text in Phase 1 (LLM connects in Phase 4)
        await update.message.reply_text(
            f"Received: \"{text}\"\n\n_(Note: AI reasoning & conversational tools activate in upcoming phases. Use /status or /audit)_",
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
