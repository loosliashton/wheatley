"""Wheatley Self-Update Utility

Pulls the latest release/commit from the configured GitHub repository,
updates dependencies inside the virtualenv, and optionally restarts
the systemd service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

logger = logging.getLogger("wheatley.updater")


@dataclass
class UpdateCheckResult:
    has_update: bool
    current_commit: str
    current_message: str
    remote_commit: str
    remote_url: str
    branch: str
    incoming_commits: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "has_update": self.has_update,
            "current_commit": self.current_commit,
            "current_message": self.current_message,
            "remote_commit": self.remote_commit,
            "remote_url": self.remote_url,
            "branch": self.branch,
            "incoming_commits": self.incoming_commits,
            "error": self.error,
        }


@dataclass
class UpdateExecutionResult:
    success: bool
    old_commit: str
    new_commit: str
    commits_pulled: List[str]
    pip_updated: bool
    service_restarted: bool
    message: str
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "old_commit": self.old_commit,
            "new_commit": self.new_commit,
            "commits_pulled": self.commits_pulled,
            "pip_updated": self.pip_updated,
            "service_restarted": self.service_restarted,
            "message": self.message,
            "error": self.error,
        }


class WheatleyUpdater:
    def __init__(self, repo_dir: Optional[Path | str] = None):
        self.repo_dir = self._resolve_repo_dir(repo_dir)

    @staticmethod
    def _resolve_repo_dir(custom_path: Optional[Path | str] = None) -> Path:
        if custom_path:
            p = Path(custom_path).resolve()
            if p.is_dir():
                return p
            raise FileNotFoundError(f"Specified repository directory does not exist: {custom_path}")

        # 1. Check parent of current package (standard editable or cloned repo structure)
        pkg_root = Path(__file__).resolve().parent.parent
        if (pkg_root / ".git").is_dir():
            return pkg_root

        # 2. Check standard production path /opt/wheatley
        opt_path = Path("/opt/wheatley")
        if (opt_path / ".git").is_dir():
            return opt_path

        # 3. Fallback to current working directory if inside git
        cwd = Path.cwd().resolve()
        if (cwd / ".git").is_dir():
            return cwd

        # Fall back to pkg_root anyway (validation will report error)
        return pkg_root

    def _run_git(self, args: List[str], check: bool = True) -> Tuple[int, str, str]:
        cmd = ["git", "-c", "safe.directory=*", "-C", str(self.repo_dir)] + args
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            err_msg = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(f"Git command failed ({' '.join(args)}): {err_msg}")
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def is_git_repo(self) -> bool:
        if not shutil.which("git"):
            return False
        ret, out, _ = self._run_git(["rev-parse", "--is-inside-work-tree"], check=False)
        return ret == 0 and out == "true"

    def get_remote_url(self, remote: str = "origin") -> str:
        ret, out, _ = self._run_git(["config", "--get", f"remote.{remote}.url"], check=False)
        return out if ret == 0 else ""

    def get_current_branch(self) -> str:
        ret, out, _ = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return out if ret == 0 else "main"

    def get_current_commit(self) -> Tuple[str, str]:
        ret, out, _ = self._run_git(["log", "-1", "--format=%h%x00%s (%cr)"], check=False)
        if ret == 0 and "\x00" in out:
            parts = out.split("\x00", 1)
            return parts[0], parts[1]
        ret2, out2, _ = self._run_git(["rev-parse", "--short", "HEAD"], check=False)
        return (out2 if ret2 == 0 else "unknown"), ""

    def has_local_changes(self) -> bool:
        ret, out, _ = self._run_git(["status", "--porcelain"], check=False)
        return bool(out.strip())

    def check_for_updates(
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
    ) -> UpdateCheckResult:
        if not self.is_git_repo():
            return UpdateCheckResult(
                has_update=False,
                current_commit="",
                current_message="",
                remote_commit="",
                remote_url="",
                branch=branch or "main",
                error=f"Directory '{self.repo_dir}' is not a git repository.",
            )

        target_branch = branch or self.get_current_branch()
        if target_branch == "HEAD":
            target_branch = "main"

        remote_url = self.get_remote_url(remote)
        curr_hash, curr_msg = self.get_current_commit()

        # Fetch latest references
        ret, _, err = self._run_git(["fetch", remote, target_branch], check=False)
        if ret != 0:
            return UpdateCheckResult(
                has_update=False,
                current_commit=curr_hash,
                current_message=curr_msg,
                remote_commit="",
                remote_url=remote_url,
                branch=target_branch,
                error=f"Failed to fetch from {remote}/{target_branch}: {err}",
            )

        # Get remote commit hash
        ret_rem, rem_hash_short, _ = self._run_git(["rev-parse", "--short", "FETCH_HEAD"], check=False)
        ret_local, local_hash_full, _ = self._run_git(["rev-parse", "HEAD"], check=False)
        ret_fetch, fetch_hash_full, _ = self._run_git(["rev-parse", "FETCH_HEAD"], check=False)

        if local_hash_full == fetch_hash_full:
            return UpdateCheckResult(
                has_update=False,
                current_commit=curr_hash,
                current_message=curr_msg,
                remote_commit=rem_hash_short if ret_rem == 0 else curr_hash,
                remote_url=remote_url,
                branch=target_branch,
            )

        # Incoming commits log
        ret_log, log_out, _ = self._run_git(
            ["log", "HEAD..FETCH_HEAD", "--oneline", "--max-count=20"], check=False
        )
        commits = [line.strip() for line in log_out.splitlines() if line.strip()]

        return UpdateCheckResult(
            has_update=True,
            current_commit=curr_hash,
            current_message=curr_msg,
            remote_commit=rem_hash_short if ret_rem == 0 else "unknown",
            remote_url=remote_url,
            branch=target_branch,
            incoming_commits=commits,
        )

    def perform_update(
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        force: bool = False,
        run_pip: bool = True,
        restart_service: bool = True,
    ) -> UpdateExecutionResult:
        if not self.is_git_repo():
            return UpdateExecutionResult(
                success=False,
                old_commit="",
                new_commit="",
                commits_pulled=[],
                pip_updated=False,
                service_restarted=False,
                message=f"Directory '{self.repo_dir}' is not a git repository.",
                error="Not a git repository",
            )

        target_branch = branch or self.get_current_branch()
        if target_branch == "HEAD":
            target_branch = "main"

        old_hash, _ = self.get_current_commit()

        # Check local changes
        if self.has_local_changes():
            if not force:
                return UpdateExecutionResult(
                    success=False,
                    old_commit=old_hash,
                    new_commit=old_hash,
                    commits_pulled=[],
                    pip_updated=False,
                    service_restarted=False,
                    message="Working directory has uncommitted modifications. Use --force to discard them.",
                    error="Dirty working directory",
                )
            # Reset hard if forced
            self._run_git(["reset", "--hard", "HEAD"], check=True)
            self._run_git(["clean", "-fd"], check=True)

        # Fetch
        ret, _, err = self._run_git(["fetch", remote, target_branch], check=False)
        if ret != 0:
            return UpdateExecutionResult(
                success=False,
                old_commit=old_hash,
                new_commit=old_hash,
                commits_pulled=[],
                pip_updated=False,
                service_restarted=False,
                message=f"Git fetch failed: {err}",
                error=err,
            )

        # Check commits incoming
        _, log_out, _ = self._run_git(
            ["log", "HEAD..FETCH_HEAD", "--oneline"], check=False
        )
        incoming = [line.strip() for line in log_out.splitlines() if line.strip()]

        # Pull / Fast-forward
        if force:
            self._run_git(["reset", "--hard", "FETCH_HEAD"], check=True)
        else:
            ret_pull, _, err_pull = self._run_git(["pull", "--ff-only", remote, target_branch], check=False)
            if ret_pull != 0:
                return UpdateExecutionResult(
                    success=False,
                    old_commit=old_hash,
                    new_commit=old_hash,
                    commits_pulled=[],
                    pip_updated=False,
                    service_restarted=False,
                    message=f"Fast-forward merge failed ({err_pull}). Use --force to align with remote.",
                    error=err_pull,
                )

        new_hash, _ = self.get_current_commit()

        # Update dependencies via pip
        pip_ok = False
        if run_pip:
            pip_ok = self._update_pip_package()

        # Restart systemd service if requested
        service_ok = False
        if restart_service:
            service_ok = self._restart_systemd_service()

        return UpdateExecutionResult(
            success=True,
            old_commit=old_hash,
            new_commit=new_hash,
            commits_pulled=incoming,
            pip_updated=pip_ok,
            service_restarted=service_ok,
            message=f"Wheatley updated to {new_hash}",
        )

    def _update_pip_package(self) -> bool:
        # Locate appropriate python executable / virtualenv
        pip_cmd = None
        venv_pip = self.repo_dir / "venv" / "bin" / "pip"
        if venv_pip.is_file() and os.access(venv_pip, os.X_OK):
            pip_cmd = [str(venv_pip)]
        elif sys.prefix != getattr(sys, "base_prefix", sys.prefix):
            pip_cmd = [sys.executable, "-m", "pip"]
        elif shutil.which("pip"):
            pip_cmd = ["pip"]

        if not pip_cmd:
            logger.warning("No pip executable found; skipping package update.")
            return False

        try:
            logger.info("Updating package via pip in %s...", self.repo_dir)
            cmd = pip_cmd + ["install", "--no-build-isolation", "-e", str(self.repo_dir)]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if res.returncode == 0:
                return True
            # Fall back without --no-build-isolation
            cmd2 = pip_cmd + ["install", "-e", str(self.repo_dir)]
            res2 = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            return res2.returncode == 0
        except Exception as e:
            logger.error("Failed to run pip update: %s", e)
            return False

    def _restart_systemd_service(self, service_name: str = "wheatley") -> bool:
        if not shutil.which("systemctl"):
            return False

        # Attempt direct restart
        try:
            res = subprocess.run(
                ["systemctl", "restart", service_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                logger.info("Successfully restarted %s.service", service_name)
                return True
        except Exception:
            pass

        # Attempt with sudo if available
        if shutil.which("sudo"):
            try:
                res_sudo = subprocess.run(
                    ["sudo", "-n", "systemctl", "restart", service_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if res_sudo.returncode == 0:
                    logger.info("Successfully restarted %s.service with sudo", service_name)
                    return True
            except Exception:
                pass

        return False


def run_cli_update(args: any) -> int:
    """Executes the `wheatley update` CLI subcommand."""
    repo_dir = getattr(args, "repo_dir", None)
    remote = getattr(args, "remote", "origin") or "origin"
    branch = getattr(args, "branch", None)
    check_only = getattr(args, "check", False)
    force = getattr(args, "force", False)
    no_restart = getattr(args, "no_restart", False)
    no_pip = getattr(args, "no_pip", False)
    as_json = getattr(args, "json", False)

    try:
        updater = WheatleyUpdater(repo_dir=repo_dir)
    except Exception as e:
        if as_json:
            print(json.dumps({"success": False, "error": str(e)}))
        else:
            print(f"❌ Error initializing updater: {e}", file=sys.stderr)
        return 1

    if not updater.is_git_repo():
        msg = f"Directory '{updater.repo_dir}' is not a git repository. Cannot update."
        if as_json:
            print(json.dumps({"success": False, "error": msg}))
        else:
            print(f"❌ {msg}", file=sys.stderr)
        return 1

    curr_branch = branch or updater.get_current_branch()
    remote_url = updater.get_remote_url(remote) or remote

    if not as_json:
        print(f"🔍 Checking for Wheatley updates from {remote_url} (branch: {curr_branch})...")

    check_res = updater.check_for_updates(remote=remote, branch=curr_branch)
    if check_res.error:
        if as_json:
            print(json.dumps({"success": False, "error": check_res.error}))
        else:
            print(f"❌ Update check failed: {check_res.error}", file=sys.stderr)
        return 1

    if not check_res.has_update:
        if as_json:
            print(json.dumps({
                "success": True,
                "status": "up_to_date",
                "commit": check_res.current_commit,
                "message": "Wheatley is already up to date.",
            }))
        else:
            print(f"✨ Wheatley is already up to date! (commit: {check_res.current_commit})")
            if check_res.current_message:
                print(f"   {check_res.current_message}")
        return 0

    # Updates found
    if not as_json:
        print(f"\n📦 Found {len(check_res.incoming_commits)} new commit(s) from {remote}/{curr_branch}:")
        for c in check_res.incoming_commits[:10]:
            print(f"  • {c}")
        if len(check_res.incoming_commits) > 10:
            print(f"  • ... and {len(check_res.incoming_commits) - 10} more")

    if check_only:
        if as_json:
            print(json.dumps({
                "success": True,
                "status": "update_available",
                "current_commit": check_res.current_commit,
                "remote_commit": check_res.remote_commit,
                "commits": check_res.incoming_commits,
            }))
        else:
            print("\n💡 Run 'wheatley update' to apply these updates.")
        return 0

    if not as_json:
        print(f"\n⬇️ Pulling latest changes into {updater.repo_dir}...")

    exec_res = updater.perform_update(
        remote=remote,
        branch=curr_branch,
        force=force,
        run_pip=not no_pip,
        restart_service=not no_restart,
    )

    if not exec_res.success:
        if as_json:
            print(json.dumps(exec_res.to_dict()))
        else:
            print(f"❌ Update failed: {exec_res.message}", file=sys.stderr)
            if exec_res.error:
                print(f"   Details: {exec_res.error}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(exec_res.to_dict()))
    else:
        print(f"📦 Python package update: {'Completed' if exec_res.pip_updated else 'Skipped'}")
        if not no_restart:
            if exec_res.service_restarted:
                print("🔄 wheatley.service restarted successfully.")
            else:
                print("⚠️ Note: wheatley.service was not restarted (run 'sudo systemctl restart wheatley' if needed).")
        print(f"\n🎉 Wheatley successfully updated from {exec_res.old_commit} to {exec_res.new_commit}!")

    return 0
