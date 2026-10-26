"""Unit tests for Telegram Allowlist Management & Interactive Proposal Buttons"""

from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram import Update, User, Message, Chat, CallbackQuery
from telegram.ext import ContextTypes

from wheatley.approvals import ApprovalStore
from wheatley.audit import AuditLogger
from wheatley.chat.telegram import TelegramBot
from wheatley.config import TelegramConfig
from wheatley.tools.shell import ShellResult


@pytest.fixture
def mock_bot(tmp_path):
    audit = AuditLogger(tmp_path / "audit.db")
    approvals = ApprovalStore(str(tmp_path / "audit.db"))
    cfg = TelegramConfig(bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", allowed_user_ids=[12345])
    bot = TelegramBot(config=cfg, audit=audit, approvals=approvals)
    bot.shell = MagicMock()
    return bot


@pytest.mark.asyncio
async def test_send_allowlist_proposal(mock_bot):
    mock_bot._send_or_reply = AsyncMock(return_value=[MagicMock()])

    await mock_bot.send_proposal(
        proposal_id="prop_test123",
        summary="Check SMART disk data",
        command="smartctl -a /dev/sda",
        target="cave",
        risk="LOW",
        proposal_type="ALLOWLIST_RULE",
        pattern="smartctl -a *",
    )

    mock_bot._send_or_reply.assert_called_once()
    _, kwargs = mock_bot._send_or_reply.call_args
    keyboard = kwargs["reply_markup"]
    assert keyboard is not None
    buttons = keyboard.inline_keyboard
    # Row 1: [Run Once, Always Allow on cave]
    # Row 2: [Deny]
    assert len(buttons) == 2
    assert buttons[0][0].text == "⚡ Run Once"
    assert buttons[0][0].callback_data == "allow_once:prop_test123"
    assert "Always Allow on cave" in buttons[0][1].text
    assert buttons[0][1].callback_data == "allow_always:prop_test123"
    assert buttons[1][0].text == "❌ Deny"
    assert buttons[1][0].callback_data == "deny:prop_test123"


@pytest.mark.asyncio
async def test_cmd_allowlist_empty_and_populated(mock_bot):
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345
    update.message = MagicMock(spec=Message)

    mock_bot._send_or_reply = AsyncMock()
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.args = []

    # 1. Empty state
    await mock_bot._cmd_allowlist(update, context)
    mock_bot._send_or_reply.assert_called_once()
    assert "No custom allowlist rules configured" in mock_bot._send_or_reply.call_args[0][0]

    # 2. Add proposal and resolve with ALLOW_ALWAYS
    prop = mock_bot.approvals.create_proposal(
        proposal_type="ALLOWLIST_RULE",
        target="cave/101",
        command="snapraid status",
        summary="snapraid rule",
        pattern="snapraid status *",
    )
    mock_bot.approvals.resolve_proposal(prop.id, "ALLOW_ALWAYS", user_id=12345)

    # 3. List rules
    mock_bot._send_or_reply.reset_mock()
    await mock_bot._cmd_allowlist(update, context)
    msg = mock_bot._send_or_reply.call_args[0][0]
    assert "Active User Allowlist Rules (1)" in msg
    assert "cave/101" in msg
    assert "snapraid status *" in msg


@pytest.mark.asyncio
async def test_callback_allow_once(mock_bot):
    prop = mock_bot.approvals.create_proposal(
        proposal_type="ALLOWLIST_RULE",
        target="cave",
        command="smartctl -a /dev/sda",
        summary="Check disk smart status",
        pattern="smartctl -a *",
    )
    mock_bot.shell.execute_on_host = AsyncMock(
        return_value=ShellResult(
            command="smartctl -a /dev/sda", exit_code=0, stdout="PASSED", stderr="", timed_out=False
        )
    )

    query = MagicMock(spec=CallbackQuery)
    query.data = f"allow_once:{prop.id}"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = MagicMock()

    update = MagicMock(spec=Update)
    update.callback_query = query
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345

    mock_bot._send_or_reply = AsyncMock()
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    await mock_bot._handle_callback(update, context)

    # Verify proposal status updated
    updated_prop = mock_bot.approvals.get_proposal(prop.id)
    assert updated_prop.status == "APPROVED"
    assert updated_prop.executed_at is not None
    # Verify execute_on_host called
    mock_bot.shell.execute_on_host.assert_called_once_with("cave", "smartctl -a /dev/sda", timeout=30)


@pytest.mark.asyncio
async def test_callback_resumes_agent_with_output(mock_bot):
    prop = mock_bot.approvals.create_proposal(
        proposal_type="ALLOWLIST_RULE",
        target="cave/101",
        command="systemctl list-timers",
        summary="List timers",
        pattern="systemctl list-timers *",
    )
    mock_bot.shell.execute_in_guest = AsyncMock(
        return_value=ShellResult(
            command="systemctl list-timers", exit_code=0, stdout="snapraid.timer active", stderr="", timed_out=False
        )
    )

    mock_agent = MagicMock()
    mock_agent.is_configured = True
    mock_agent.chat = AsyncMock(return_value="Based on the timer output, SnapRAID ran successfully.")
    mock_bot.agent = mock_agent

    query = MagicMock(spec=CallbackQuery)
    query.data = f"allow_once:{prop.id}"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = MagicMock()
    query.message.chat_id = 12345

    update = MagicMock(spec=Update)
    update.callback_query = query
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345

    mock_bot._send_or_reply = AsyncMock()
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    await mock_bot._handle_callback(update, context)

    # Verify agent was invoked to continue the inquiry with output
    mock_agent.chat.assert_called_once()
    continuation_prompt = mock_agent.chat.call_args[0][0]
    assert "systemctl list-timers" in continuation_prompt
    assert "snapraid.timer active" in continuation_prompt
    # Verify the agent's interpretation was sent to user
    sent_messages = [call[0][0] for call in mock_bot._send_or_reply.call_args_list]
    assert any("Based on the timer output" in m for m in sent_messages)
