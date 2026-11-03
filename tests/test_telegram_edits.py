"""Unit tests for Telegram in-place message edits reducing chat clutter."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram import Update, User, CallbackQuery
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from wheatley.approvals import ApprovalStore
from wheatley.audit import AuditLogger
from wheatley.chat.telegram import TelegramBot
from wheatley.config import TelegramConfig
from wheatley.tools.shell import ShellResult


@pytest.fixture
def bot(tmp_path):
    audit = AuditLogger(tmp_path / "audit.db")
    approvals = ApprovalStore(str(tmp_path / "audit.db"))
    cfg = TelegramConfig(bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", allowed_user_ids=[12345])
    b = TelegramBot(config=cfg, audit=audit, approvals=approvals)
    b.shell = MagicMock()
    return b


@pytest.mark.asyncio
async def test_edit_or_send_edits_existing_message(bot):
    msg = MagicMock()
    msg.edit_text = AsyncMock(return_value=msg)

    res = await bot._edit_or_send("*Updated status*", message=msg)

    assert len(res) == 1
    msg.edit_text.assert_called_once()
    args, kwargs = msg.edit_text.call_args
    assert "<b>Updated status</b>" in args[0] or "Updated status" in args[0]
    assert kwargs["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_edit_or_send_handles_message_not_modified(bot):
    msg = MagicMock()
    msg.edit_text = AsyncMock(side_effect=Exception("BadRequest: message is not modified"))
    msg.edit_reply_markup = AsyncMock()

    res = await bot._edit_or_send("Same content", message=msg)

    assert len(res) == 1
    assert res[0] is msg


@pytest.mark.asyncio
async def test_edit_or_send_fallback_when_edit_fails(bot):
    msg = MagicMock()
    msg.edit_text = AsyncMock(side_effect=Exception("Message to edit not found"))
    bot._send_or_reply = AsyncMock(return_value=[MagicMock()])

    res = await bot._edit_or_send("Fallback text", message=msg)

    bot._send_or_reply.assert_called_once()
    assert len(res) == 1


@pytest.mark.asyncio
async def test_deny_proposal_edits_card_in_place(bot):
    prop = bot.approvals.create_proposal(
        proposal_type="ACTION",
        target="cave/100",
        command="systemctl restart docker",
        summary="Restart docker service",
        risk="HIGH",
    )

    query = MagicMock(spec=CallbackQuery)
    query.data = f"deny:{prop.id}"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = MagicMock()
    query.message.edit_text = AsyncMock(return_value=query.message)
    query.message.chat_id = 12345

    update = MagicMock(spec=Update)
    update.callback_query = query
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    await bot._handle_callback(update, context)

    # Verify proposal status is DENIED in DB
    updated_prop = bot.approvals.get_proposal(prop.id)
    assert updated_prop.status == "DENIED"

    # Verify card was edited with Denied status and no reply_markup
    query.message.edit_text.assert_called()
    called_text = query.message.edit_text.call_args[0][0]
    assert "Denied by user" in called_text


@pytest.mark.asyncio
async def test_approve_proposal_edits_card_in_place(bot):
    prop = bot.approvals.create_proposal(
        proposal_type="ACTION",
        target="pve-node-01",
        command="uptime",
        summary="Check uptime",
        risk="LOW",
    )

    bot.shell.execute_on_host = AsyncMock(
        return_value=ShellResult(command="uptime", exit_code=0, stdout="up 14 days", stderr="", timed_out=False)
    )

    query = MagicMock(spec=CallbackQuery)
    query.data = f"approve:{prop.id}"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = MagicMock()
    query.message.edit_text = AsyncMock(return_value=query.message)
    query.message.chat_id = 12345

    update = MagicMock(spec=Update)
    update.callback_query = query
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    await bot._handle_callback(update, context)

    updated_prop = bot.approvals.get_proposal(prop.id)
    assert updated_prop.status == "APPROVED"
    assert updated_prop.executed_at is not None

    # Verify proposal card was edited to approved
    query.message.edit_text.assert_called()
    last_text = query.message.edit_text.call_args[0][0]
    assert "Approved &amp; Executed" in last_text or "Approved & Executed" in last_text


@pytest.mark.asyncio
async def test_mute_alert_edits_message_in_place(bot):
    mock_engine = MagicMock()
    from datetime import datetime, timezone, timedelta
    mock_engine.mute_check.return_value = datetime.now(timezone.utc) + timedelta(minutes=60)
    bot.check_engine = mock_engine

    query = MagicMock(spec=CallbackQuery)
    query.data = "mute:web-app:60"
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.text = "🚨 *CRITICAL ALERT:* `web-app` returned 503"
    query.message.edit_text = AsyncMock(return_value=query.message)

    update = MagicMock(spec=Update)
    update.callback_query = query
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    await bot._handle_callback(update, context)

    mock_engine.mute_check.assert_called_once_with("web-app", 60)
    query.message.edit_text.assert_called_once()
    called_text = query.message.edit_text.call_args[0][0]
    assert "Muted for 60m" in called_text
    # Verify Investigate button is preserved on keyboard
    called_markup = query.message.edit_text.call_args[1]["reply_markup"]
    assert called_markup is not None
    assert called_markup.inline_keyboard[0][0].callback_data == "investigate:web-app"


@pytest.mark.asyncio
async def test_cmd_discover_edits_placeholder(bot):
    bot.discovery_provider = AsyncMock(return_value="Discovered 3 nodes and 12 LXC guests.")
    bot._send_or_reply = AsyncMock()

    placeholder_msg = MagicMock()
    placeholder_msg.edit_text = AsyncMock(return_value=placeholder_msg)
    bot._send_or_reply.return_value = [placeholder_msg]

    update = MagicMock(spec=Update)
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345
    update.message = MagicMock()
    update.effective_chat = MagicMock()

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    await bot._cmd_discover(update, context)

    bot._send_or_reply.assert_called_once()
    placeholder_msg.edit_text.assert_called_once()
    edited_text = placeholder_msg.edit_text.call_args[0][0]
    assert "Discovered 3 nodes" in edited_text

