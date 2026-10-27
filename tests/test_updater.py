"""Unit tests for WheatleyUpdater and `wheatley update` CLI command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
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
