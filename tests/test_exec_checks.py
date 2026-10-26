"""Unit tests for Exec-based Check Plugins (Docker, Systemd, Command, LvmThin, Zfs, Smart, Sensors)"""

import json
from unittest.mock import AsyncMock, MagicMock
import pytest

from wheatley.checks.base import CheckContext, CheckStatus
from wheatley.checks.builtin.command import CommandCheck
from wheatley.checks.builtin.docker import DockerCheck
from wheatley.checks.builtin.lvm_thin import LvmThinCheck
from wheatley.checks.builtin.sensors import SensorsCheck
from wheatley.checks.builtin.smart import SmartCheck
from wheatley.checks.builtin.systemd import SystemdCheck
from wheatley.checks.builtin.zfs import ZfsCheck
from wheatley.config import NodeConfig, NodeSshConfig, NodeTokensConfig, WheatleyConfig
from wheatley.tools.shell import ShellResult


@pytest.fixture
def mock_context():
    config = WheatleyConfig(
        nodes=[
            NodeConfig(
                name="cave",
                api_url="https://192.168.1.111:8006",
                ssh=NodeSshConfig(host="192.168.1.111", port=22, user="wheatley"),
                tokens=NodeTokensConfig(monitor="wheatley@pve!bot=sec", ops="wheatley@pve!ops=sec"),
            )
        ],
        chat={"telegram": {"bot_token": "tok", "allowed_user_ids": [123]}},
    )
    shell_mock = MagicMock()
    ctx = CheckContext(config, {}, shell=shell_mock)
    return ctx


@pytest.mark.asyncio
async def test_docker_check_native_guest(mock_context):
    check = DockerCheck(
        id="docker-test",
        type="docker",
        params={"guest": "cave/100", "alert_on": ["exited", "unhealthy"]},
    )

    # Mock docker ps returning lines of JSON
    docker_output = (
        '{"Names": "plex", "State": "running", "Status": "Up 2 days (healthy)"}\n'
        '{"Names": "frigate", "State": "running", "Status": "Up 2 days"}\n'
    )
    mock_context.shell.execute_guest_spec = AsyncMock(
        return_value=ShellResult(
            command="docker ps", exit_code=0, stdout=docker_output, stderr=""
        )
    )

    res = await check.run(mock_context)
    assert res.status == CheckStatus.OK
    assert "All 2 Docker containers healthy" in res.summary


@pytest.mark.asyncio
async def test_docker_check_alerts_on_issue(mock_context):
    check = DockerCheck(
        id="docker-test",
        type="docker",
        params={"guest": "cave/100", "alert_on": ["exited", "unhealthy"]},
    )

    docker_output = (
        '{"Names": "plex", "State": "exited", "Status": "Exited (1) 5 minutes ago"}\n'
        '{"Names": "frigate", "State": "running", "Status": "Up 2 days"}\n'
    )
    mock_context.shell.execute_guest_spec = AsyncMock(
        return_value=ShellResult(
            command="docker ps", exit_code=0, stdout=docker_output, stderr=""
        )
    )

    res = await check.run(mock_context)
    assert res.status == CheckStatus.CRIT
    assert "Container 'plex' is EXITED" in res.summary


@pytest.mark.asyncio
async def test_systemd_check(mock_context):
    check = SystemdCheck(
        id="sysd-test",
        type="systemd",
        params={"node": "cave", "units": ["frigate", "pveproxy"]},
    )

    mock_context.shell.execute_on_host = AsyncMock(
        side_effect=[
            ShellResult(command="systemctl is-active frigate.service", exit_code=0, stdout="active\n", stderr=""),
            ShellResult(command="systemctl is-active pveproxy.service", exit_code=3, stdout="failed\n", stderr=""),
        ]
    )

    res = await check.run(mock_context)
    assert res.status == CheckStatus.CRIT
    assert "pveproxy.service is FAILED" in res.summary


@pytest.mark.asyncio
async def test_command_check_security_and_execution(mock_context):
    # Dangerous command must be blocked by allowlist
    blocked_check = CommandCheck(
        id="cmd-blocked",
        type="command",
        params={"node": "cave", "command": "reboot"},
    )
    res_blocked = await blocked_check.run(mock_context)
    assert res_blocked.status == CheckStatus.CRIT
    assert "Security violation" in res_blocked.summary

    # Valid command matching output
    valid_check = CommandCheck(
        id="cmd-ok",
        type="command",
        params={"node": "cave", "command": "uptime", "output_regex": r"load average"},
    )
    mock_context.shell.execute_on_host = AsyncMock(
        return_value=ShellResult(
            command="uptime", exit_code=0, stdout="up 2 days, load average: 0.1, 0.2, 0.3", stderr=""
        )
    )
    res_ok = await valid_check.run(mock_context)
    assert res_ok.status == CheckStatus.OK


@pytest.mark.asyncio
async def test_lvm_thin_check(mock_context):
    check = LvmThinCheck(
        id="lvm-test",
        type="lvm_thin",
        params={"nodes": ["cave"], "warn_percent": 80, "crit_percent": 90},
    )

    pvesm_output = (
        "Name             Type     Status     Total (KiB)      Used (KiB) Available (KiB)        %\n"
        "local             dir     active        98497780        62430256        31017976   63.38%\n"
        "local-lvm     lvmthin     active       832888832       791244390        41644442   95.00%\n"
    )
    mock_context.shell.execute_on_host = AsyncMock(
        return_value=ShellResult(command="pvesm status", exit_code=0, stdout=pvesm_output, stderr="")
    )

    res = await check.run(mock_context)
    assert res.status == CheckStatus.CRIT
    assert "95.0% capacity (CRIT" in res.summary


@pytest.mark.asyncio
async def test_zfs_check(mock_context):
    check = ZfsCheck(id="zfs-test", type="zfs", params={"nodes": ["cave"]})

    mock_context.shell.execute_on_host = AsyncMock(
        return_value=ShellResult(command="zpool status -x", exit_code=0, stdout="all pools are healthy\n", stderr="")
    )

    res = await check.run(mock_context)
    assert res.status == CheckStatus.OK
    assert "All ZFS pools healthy" in res.summary

    # Degraded pool test
    mock_context.shell.execute_on_host = AsyncMock(
        return_value=ShellResult(command="zpool status -x", exit_code=0, stdout="pool 'tank' is degraded\n", stderr="")
    )
    res_crit = await check.run(mock_context)
    assert res_crit.status == CheckStatus.CRIT
    assert "ZFS degradation detected" in res_crit.summary


@pytest.mark.asyncio
async def test_smart_check(mock_context):
    check = SmartCheck(
        id="smart-test",
        type="smart",
        params={"node": "cave", "devices": ["/dev/nvme0n1", "/dev/sda"]},
    )

    mock_context.shell.execute_on_host = AsyncMock(
        side_effect=[
            ShellResult(command="smartctl -H /dev/nvme0n1", exit_code=0, stdout="test result: PASSED", stderr=""),
            ShellResult(command="smartctl -H /dev/sda", exit_code=8, stdout="test result: FAILED", stderr=""),
        ]
    )

    res = await check.run(mock_context)
    assert res.status == CheckStatus.CRIT
    assert "/dev/sda SMART health FAILED" in res.summary


@pytest.mark.asyncio
async def test_sensors_check(mock_context):
    check = SensorsCheck(
        id="sensors-test",
        type="sensors",
        params={"node": "cave", "warn_temp_c": 75, "crit_temp_c": 85},
    )

    sensors_json = {
        "coretemp-isa-0000": {
            "Package id 0": {"temp1_input": 50.0},
            "Core 0": {"temp2_input": 45.0},
        }
    }
    mock_context.shell.execute_on_host = AsyncMock(
        return_value=ShellResult(command="sensors -j", exit_code=0, stdout=json.dumps(sensors_json), stderr="")
    )

    res = await check.run(mock_context)
    assert res.status == CheckStatus.OK
    assert "Peak: 50.0°C" in res.summary
