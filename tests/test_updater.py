"""Unit tests for WheatleyUpdater and `wheatley update` CLI command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from wheatley.updater import UpdateCheckResult, WheatleyUpdater, run_cli_update


def test_updater_resolve_repo_dir_custom(tmp_path: Path):
    custom_dir = tmp_path / "custom_wheatley"
    custom_dir.mkdir()
    updater = WheatleyUpdater(repo_dir=custom_dir)
    assert updater.repo_dir == custom_dir


def test_updater_resolve_repo_dir_nonexistent():
    with pytest.raises(FileNotFoundError):
        WheatleyUpdater(repo_dir="/nonexistent/path/wheatley_xyz")


def test_updater_is_git_repo(tmp_path: Path):
    non_git = tmp_path / "nongit"
    non_git.mkdir()
    updater = WheatleyUpdater(repo_dir=non_git)
    assert updater.is_git_repo() is False


def test_updater_check_for_updates_up_to_date(tmp_path: Path):
    updater = WheatleyUpdater(repo_dir=tmp_path)
    with patch.object(updater, "is_git_repo", return_value=True), \
         patch.object(updater, "get_current_branch", return_value="main"), \
         patch.object(updater, "get_remote_url", return_value="https://github.com/loosliashton/wheatley.git"), \
         patch.object(updater, "get_current_commit", return_value=("abc1234", "Initial commit")), \
         patch.object(updater, "_run_git") as mock_git:

        # Mock: fetch succeeds (code 0)
        # rev-parse FETCH_HEAD (short) -> abc1234
        # rev-parse HEAD (full) -> 1111111111111111111111111111111111111111
        # rev-parse FETCH_HEAD (full) -> 1111111111111111111111111111111111111111
        def git_side_effect(args, check=True):
            if args[:2] == ["fetch", "origin"]:
                return 0, "", ""
            if args == ["rev-parse", "--short", "FETCH_HEAD"]:
                return 0, "abc1234", ""
            if args == ["rev-parse", "HEAD"]:
                return 0, "1111111111111111111111111111111111111111", ""
            if args == ["rev-parse", "FETCH_HEAD"]:
                return 0, "1111111111111111111111111111111111111111", ""
            return 0, "", ""

        mock_git.side_effect = git_side_effect

        res = updater.check_for_updates()
        assert res.has_update is False
        assert res.current_commit == "abc1234"
        assert res.error is None


def test_updater_check_for_updates_available(tmp_path: Path):
    updater = WheatleyUpdater(repo_dir=tmp_path)
    with patch.object(updater, "is_git_repo", return_value=True), \
         patch.object(updater, "get_current_branch", return_value="main"), \
         patch.object(updater, "get_remote_url", return_value="https://github.com/loosliashton/wheatley.git"), \
         patch.object(updater, "get_current_commit", return_value=("abc1234", "Old commit")), \
         patch.object(updater, "_run_git") as mock_git:

        def git_side_effect(args, check=True):
            if args[:2] == ["fetch", "origin"]:
                return 0, "", ""
            if args == ["rev-parse", "--short", "FETCH_HEAD"]:
                return 0, "def5678", ""
            if args == ["rev-parse", "HEAD"]:
                return 0, "1111111111111111111111111111111111111111", ""
            if args == ["rev-parse", "FETCH_HEAD"]:
                return 0, "2222222222222222222222222222222222222222", ""
            if args[:2] == ["log", "HEAD..FETCH_HEAD"]:
                return 0, "def5678 feat: new update\n9999999 fix: bugfix", ""
            return 0, "", ""

        mock_git.side_effect = git_side_effect

        res = updater.check_for_updates()
        assert res.has_update is True
        assert res.current_commit == "abc1234"
        assert res.remote_commit == "def5678"
        assert len(res.incoming_commits) == 2
        assert "def5678 feat: new update" in res.incoming_commits


def test_updater_perform_update_dirty_tree_blocked(tmp_path: Path):
    updater = WheatleyUpdater(repo_dir=tmp_path)
    with patch.object(updater, "is_git_repo", return_value=True), \
         patch.object(updater, "has_local_changes", return_value=True), \
         patch.object(updater, "get_current_commit", return_value=("abc1234", "Commit")):

        res = updater.perform_update(force=False)
        assert res.success is False
        assert "uncommitted modifications" in res.message


def test_updater_perform_update_success(tmp_path: Path):
    updater = WheatleyUpdater(repo_dir=tmp_path)
    with patch.object(updater, "is_git_repo", return_value=True), \
         patch.object(updater, "has_local_changes", return_value=False), \
         patch.object(updater, "get_current_branch", return_value="main"), \
         patch.object(updater, "_update_pip_package", return_value=True), \
         patch.object(updater, "_restart_systemd_service", return_value=True), \
         patch.object(updater, "_run_git") as mock_git:

        commits_sequence = [("abc1234", "Old commit"), ("def5678", "New commit")]
        call_count = {"commit": 0}

        def get_commit():
            idx = min(call_count["commit"], len(commits_sequence) - 1)
            call_count["commit"] += 1
            return commits_sequence[idx]

        updater.get_current_commit = get_commit

        def git_side_effect(args, check=True):
            if args[:2] == ["fetch", "origin"]:
                return 0, "", ""
            if args[:2] == ["log", "HEAD..FETCH_HEAD"]:
                return 0, "def5678 New commit", ""
            if args[:3] == ["pull", "--ff-only", "origin"]:
                return 0, "", ""
            return 0, "", ""

        mock_git.side_effect = git_side_effect

        res = updater.perform_update(force=False, run_pip=True, restart_service=True)
        assert res.success is True
        assert res.old_commit == "abc1234"
        assert res.new_commit == "def5678"
        assert res.pip_updated is True
        assert res.service_restarted is True
        assert len(res.commits_pulled) == 1


def test_cli_update_check_flag(capsys):
    mock_updater = MagicMock()
    mock_updater.is_git_repo.return_value = True
    mock_updater.get_current_branch.return_value = "main"
    mock_updater.get_remote_url.return_value = "https://github.com/loosliashton/wheatley.git"
    mock_updater.check_for_updates.return_value = UpdateCheckResult(
        has_update=True,
        current_commit="abc1234",
        current_message="Old",
        remote_commit="def5678",
        remote_url="https://github.com/loosliashton/wheatley.git",
        branch="main",
        incoming_commits=["def5678 New commit"],
    )

    args = argparse.Namespace(
        command="update",
        check=True,
        branch=None,
        remote="origin",
        repo_dir=None,
        no_restart=False,
        no_pip=False,
        force=False,
        json=False,
    )

    with patch("wheatley.updater.WheatleyUpdater", return_value=mock_updater):
        ret = run_cli_update(args)
        assert ret == 0
        mock_updater.perform_update.assert_not_called()
        captured = capsys.readouterr()
        assert "Found 1 new commit" in captured.out
        assert "Run 'wheatley update' to apply" in captured.out


def test_cli_update_json_output(capsys):
    mock_updater = MagicMock()
    mock_updater.is_git_repo.return_value = True
    mock_updater.get_current_branch.return_value = "main"
    mock_updater.get_remote_url.return_value = "https://github.com/loosliashton/wheatley.git"
    mock_updater.check_for_updates.return_value = UpdateCheckResult(
        has_update=False,
        current_commit="abc1234",
        current_message="Initial",
        remote_commit="abc1234",
        remote_url="https://github.com/loosliashton/wheatley.git",
        branch="main",
    )

    args = argparse.Namespace(
        command="update",
        check=True,
        branch=None,
        remote="origin",
        repo_dir=None,
        no_restart=False,
        no_pip=False,
        force=False,
        json=True,
    )

    with patch("wheatley.updater.WheatleyUpdater", return_value=mock_updater):
        ret = run_cli_update(args)
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "up_to_date"
        assert data["commit"] == "abc1234"


def test_update_config_defaults():
    from wheatley.config import UpdateConfig
    cfg = UpdateConfig()
    assert cfg.enabled is True
    assert cfg.interval_hours == 24.0
    assert cfg.check_on_startup is True
    assert cfg.startup_delay_seconds == 30.0
    assert cfg.interval_seconds == 86400.0
    assert cfg.remote == "origin"
    assert cfg.branch is None


def test_update_config_aliases():
    from wheatley.config import UpdateConfig
    cfg1 = UpdateConfig.model_validate({"check_interval_hours": 12})
    assert cfg1.interval_hours == 12.0
    assert cfg1.interval_seconds == 43200.0

    cfg2 = UpdateConfig.model_validate({"interval_seconds": 3600})
    assert cfg2.interval_hours == 1.0
    assert cfg2.interval_seconds == 3600.0

    cfg3 = UpdateConfig.model_validate({"check_interval_seconds": 7200})
    assert cfg3.interval_hours == 2.0


def test_wheatley_config_updates_section(tmp_path: Path):
    from wheatley.config import load_config
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
nodes: []
chat:
  telegram:
    bot_token: "dummy"
    allowed_user_ids: [12345]
updates:
  enabled: true
  interval_hours: 6
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.updates.enabled is True
    assert cfg.updates.interval_hours == 6.0


def test_wheatley_config_update_alias_section(tmp_path: Path):
    from wheatley.config import load_config
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
nodes: []
chat:
  telegram:
    bot_token: "dummy"
    allowed_user_ids: [12345]
update:
  enabled: false
  interval_hours: 12
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.updates.enabled is False
    assert cfg.updates.interval_hours == 12.0


@pytest.mark.asyncio
async def test_update_checker_disabled():
    from wheatley.config import UpdateConfig
    from wheatley.updater import UpdateChecker

    mock_updater = MagicMock()
    mock_bot = MagicMock()
    checker = UpdateChecker(
        config=UpdateConfig(enabled=False),
        updater=mock_updater,
        bot=mock_bot,
    )
    res = await checker.check_and_notify()
    assert res is None
    mock_updater.check_for_updates.assert_not_called()
    mock_bot.send_update_proposal.assert_not_called()


@pytest.mark.asyncio
async def test_update_checker_no_bot():
    from wheatley.config import UpdateConfig
    from wheatley.updater import UpdateChecker

    mock_updater = MagicMock()
    checker = UpdateChecker(
        config=UpdateConfig(enabled=True),
        updater=mock_updater,
        bot=None,
    )
    res = await checker.check_and_notify()
    assert res is None
    mock_updater.check_for_updates.assert_not_called()


@pytest.mark.asyncio
async def test_update_checker_up_to_date():
    from wheatley.config import UpdateConfig
    from wheatley.updater import UpdateChecker

    mock_updater = MagicMock()
    mock_updater.check_for_updates.return_value = UpdateCheckResult(
        has_update=False,
        current_commit="abc1234",
        current_message="Latest",
        remote_commit="abc1234",
        remote_url="https://github.com/loosliashton/wheatley.git",
        branch="main",
    )
    mock_bot = MagicMock()
    mock_bot.send_update_proposal = AsyncMock()

    checker = UpdateChecker(
        config=UpdateConfig(enabled=True),
        updater=mock_updater,
        bot=mock_bot,
    )
    res = await checker.check_and_notify()
    assert res is not None
    assert res.has_update is False
    mock_bot.send_update_proposal.assert_not_called()
    assert checker.last_notified_commit is None


@pytest.mark.asyncio
async def test_update_checker_notifies_when_update_available(tmp_path: Path):
    from unittest.mock import AsyncMock
    from wheatley.audit import AuditLogger
    from wheatley.config import UpdateConfig
    from wheatley.updater import UpdateChecker

    mock_updater = MagicMock()
    check_result = UpdateCheckResult(
        has_update=True,
        current_commit="abc1234",
        current_message="Old commit",
        remote_commit="def5678",
        remote_url="https://github.com/loosliashton/wheatley.git",
        branch="main",
        incoming_commits=["def5678 New feature", "7890abc Bug fix"],
    )
    mock_updater.check_for_updates.return_value = check_result

    audit = AuditLogger(tmp_path / "test_audit.db")
    mock_bot = MagicMock()
    mock_bot.send_update_proposal = AsyncMock(return_value=MagicMock())

    checker = UpdateChecker(
        config=UpdateConfig(enabled=True, interval_hours=12.0),
        updater=mock_updater,
        bot=mock_bot,
        audit=audit,
    )

    res = await checker.check_and_notify()
    assert res == check_result
    mock_bot.send_update_proposal.assert_called_once_with(
        check_result,
        expires_minutes=1440,
    )
    assert checker.last_notified_commit == "def5678"

    # Verify audit event
    logs = audit.get_recent_logs(limit=5)
    assert any(log["action"] == "update_notification_sent" for log in logs)


@pytest.mark.asyncio
async def test_update_checker_deduplication():
    from unittest.mock import AsyncMock
    from wheatley.config import UpdateConfig
    from wheatley.updater import UpdateChecker

    mock_updater = MagicMock()
    check_result1 = UpdateCheckResult(
        has_update=True,
        current_commit="abc1234",
        current_message="Old commit",
        remote_commit="def5678",
        remote_url="https://github.com/loosliashton/wheatley.git",
        branch="main",
        incoming_commits=["def5678 New feature"],
    )
    mock_updater.check_for_updates.return_value = check_result1

    mock_bot = MagicMock()
    mock_bot.send_update_proposal = AsyncMock(return_value=MagicMock())

    checker = UpdateChecker(
        config=UpdateConfig(enabled=True),
        updater=mock_updater,
        bot=mock_bot,
    )

    # First check: notifies
    res1 = await checker.check_and_notify()
    assert res1.has_update is True
    assert mock_bot.send_update_proposal.call_count == 1

    # Second check with SAME remote_commit: deduplicated / skipped!
    res2 = await checker.check_and_notify()
    assert res2.has_update is True
    assert mock_bot.send_update_proposal.call_count == 1

    # Third check with NEW remote_commit: notifies again!
    check_result2 = UpdateCheckResult(
        has_update=True,
        current_commit="abc1234",
        current_message="Old commit",
        remote_commit="ghi9012",
        remote_url="https://github.com/loosliashton/wheatley.git",
        branch="main",
        incoming_commits=["def5678 New feature", "ghi9012 Another feature"],
    )
    mock_updater.check_for_updates.return_value = check_result2

    res3 = await checker.check_and_notify()
    assert res3.has_update is True
    assert mock_bot.send_update_proposal.call_count == 2
    assert checker.last_notified_commit == "ghi9012"


@pytest.mark.asyncio
async def test_update_checker_git_error_handled_gracefully():
    from unittest.mock import AsyncMock
    from wheatley.config import UpdateConfig
    from wheatley.updater import UpdateChecker

    mock_updater = MagicMock()
    mock_updater.check_for_updates.return_value = UpdateCheckResult(
        has_update=False,
        current_commit="abc1234",
        current_message="Commit",
        remote_commit="",
        remote_url="",
        branch="main",
        error="Could not resolve host: github.com",
    )
    mock_bot = MagicMock()
    mock_bot.send_update_proposal = AsyncMock()

    checker = UpdateChecker(
        config=UpdateConfig(enabled=True),
        updater=mock_updater,
        bot=mock_bot,
    )
    res = await checker.check_and_notify()
    assert res.error is not None
    mock_bot.send_update_proposal.assert_not_called()


@pytest.mark.asyncio
async def test_update_checker_lifecycle():
    import asyncio
    from wheatley.config import UpdateConfig
    from wheatley.updater import UpdateChecker

    mock_updater = MagicMock()
    mock_updater.check_for_updates.return_value = UpdateCheckResult(
        has_update=False,
        current_commit="abc1234",
        current_message="Commit",
        remote_commit="abc1234",
        remote_url="",
        branch="main",
    )
    mock_bot = MagicMock()

    cfg = UpdateConfig(
        enabled=True,
        interval_hours=0.01,
        check_on_startup=True,
        startup_delay_seconds=0.01,
    )
    checker = UpdateChecker(config=cfg, updater=mock_updater, bot=mock_bot)
    checker.start()
    assert checker._task is not None
    assert not checker._task.done()

    # Wait a moment for startup check to run
    await asyncio.sleep(0.05)
    assert mock_updater.check_for_updates.called

    await checker.stop()
    assert checker._task is None


@pytest.mark.asyncio
async def test_telegram_send_update_proposal(tmp_path: Path):
    from unittest.mock import AsyncMock
    from wheatley.approvals import ApprovalStore
    from wheatley.audit import AuditLogger
    from wheatley.chat.telegram import TelegramBot
    from wheatley.config import TelegramConfig

    audit = AuditLogger(tmp_path / "audit.db")
    approvals = ApprovalStore(str(tmp_path / "audit.db"))
    cfg = TelegramConfig(bot_token="dummy_token_123", allowed_user_ids=[12345])
    bot = TelegramBot(config=cfg, audit=audit, approvals=approvals)
    bot._send_or_reply = AsyncMock(return_value=[MagicMock()])

    check_res = UpdateCheckResult(
        has_update=True,
        current_commit="abc1234",
        current_message="Old commit",
        remote_commit="def5678",
        remote_url="https://github.com/loosliashton/wheatley.git",
        branch="main",
        incoming_commits=["def5678 Release v0.2.0", "1234567 Fix watchdog"],
    )

    await bot.send_update_proposal(check_res, expires_minutes=720)

    # Verify message sent with reply markup
    bot._send_or_reply.assert_called_once()
    args, kwargs = bot._send_or_reply.call_args
    sent_text = args[0]

    assert "WHEATLEY UPDATE PROPOSAL" in sent_text
    assert "def5678" in sent_text
    assert "Release v0.2.0" in sent_text

    # Verify buttons
    keyboard = kwargs["reply_markup"]
    assert keyboard is not None
    buttons = keyboard.inline_keyboard
    assert len(buttons) == 1
    # Buttons: [🚀 Update Now, ❌ Cancel]
    assert buttons[0][0].text == "🚀 Update Now"
    assert buttons[0][0].callback_data.startswith("approve:prop_")
    assert buttons[0][1].text == "❌ Cancel"
    assert buttons[0][1].callback_data.startswith("deny:prop_")

    # Verify proposal created in ApprovalStore
    prop_id = buttons[0][0].callback_data.split(":", 1)[1]
    prop = approvals.get_proposal(prop_id)
    assert prop is not None
    assert prop.proposal_type == "SELF_UPDATE"
    assert prop.command == "wheatley update"
    assert prop.status == "PENDING"

