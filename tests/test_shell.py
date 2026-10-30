"""Unit tests for ShellExecutor and ShellResult"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from wheatley.config import NodeConfig, NodeSshConfig, NodeTokensConfig, WheatleyConfig
from wheatley.tools.shell import ShellExecutor, ShellResult


@pytest.fixture
def test_config():
    return WheatleyConfig(
        nodes=[
            NodeConfig(
                name="pve-node-01",
                api_url="https://192.168.1.111:8006",
                ssh=NodeSshConfig(host="192.168.1.111", port=22, user="wheatley"),
                tokens=NodeTokensConfig(monitor="wheatley@pve!bot=sec", ops="wheatley@pve!ops=sec"),
            ),
            NodeConfig(
                name="pve-node-02",
                api_url="https://192.168.1.112:8006",
                ssh=NodeSshConfig(host="192.168.1.112", port=22, user="wheatley"),
                tokens=NodeTokensConfig(monitor="wheatley@pve!bot=sec", ops="wheatley@pve!ops=sec"),
            ),
        ],
        chat={"telegram": {"bot_token": "token", "allowed_user_ids": [123]}},
    )


def test_shell_result():
    res = ShellResult(command="uptime", exit_code=0, stdout="up 2 days", stderr="")
    assert res.success
    assert not res.timed_out

    fail_res = ShellResult(command="uptime", exit_code=1, stdout="", stderr="error")
    assert not fail_res.success

    timeout_res = ShellResult(command="uptime", exit_code=124, stdout="", stderr="", timed_out=True)
    assert not timeout_res.success


@pytest.mark.asyncio
async def test_execute_on_host_normal(test_config):
    executor = ShellExecutor(test_config)

    mock_run_res = MagicMock()
    mock_run_res.exit_status = 0
    mock_run_res.stdout = "up 2 days\n"
    mock_run_res.stderr = ""

    mock_conn = MagicMock()
    mock_conn.is_closed.return_value = False
    mock_conn.run = AsyncMock(return_value=mock_run_res)

    with patch("asyncssh.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn

        res = await executor.execute_on_host("pve-node-01", "uptime")
        assert res.success
        assert res.stdout == "up 2 days\n"
        mock_conn.run.assert_called_once_with("uptime", check=False)


@pytest.mark.asyncio
async def test_execute_on_host_adds_sudo_for_restricted(test_config):
    executor = ShellExecutor(test_config)

    mock_run_res = MagicMock()
    mock_run_res.exit_status = 0
    mock_run_res.stdout = "local-lvm lvmthin active ...\n"
    mock_run_res.stderr = ""

    mock_conn = MagicMock()
    mock_conn.is_closed.return_value = False
    mock_conn.run = AsyncMock(return_value=mock_run_res)

    with patch("asyncssh.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn

        res = await executor.execute_on_host("pve-node-01", "pvesm status")
        assert res.success
        # Must have prefixed sudo
        mock_conn.run.assert_called_once_with("sudo pvesm status", check=False)


@pytest.mark.asyncio
async def test_execute_in_lxc(test_config):
    executor = ShellExecutor(test_config)

    mock_run_res = MagicMock()
    mock_run_res.exit_status = 0
    mock_run_res.stdout = "active\n"
    mock_run_res.stderr = ""

    mock_conn = MagicMock()
    mock_conn.is_closed.return_value = False
    mock_conn.run = AsyncMock(return_value=mock_run_res)

    with patch("asyncssh.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn

        res = await executor.execute_in_lxc("pve-node-01", 102, "systemctl is-active nginx")
        assert res.success
        assert res.stdout == "active\n"
        expected_call = "sudo /usr/sbin/pct exec 102 -- /bin/bash -c 'systemctl is-active nginx'"
        mock_conn.run.assert_called_once_with(expected_call, check=False)


@pytest.mark.asyncio
async def test_execute_in_vm_parses_json(test_config):
    executor = ShellExecutor(test_config)

    # qm guest exec returns JSON wrapper
    qm_json_output = '{"exitcode": 0, "exited": 1, "out-data": "container1\\ncontainer2\\n"}'

    mock_run_res = MagicMock()
    mock_run_res.exit_status = 0
    mock_run_res.stdout = qm_json_output
    mock_run_res.stderr = ""

    mock_conn = MagicMock()
    mock_conn.is_closed.return_value = False
    mock_conn.run = AsyncMock(return_value=mock_run_res)

    with patch("asyncssh.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn

        res = await executor.execute_in_vm("pve-node-01", 100, "docker ps --format '{{.Names}}'")
        assert res.success
        assert res.exit_code == 0
        assert res.stdout == "container1\ncontainer2\n"


@pytest.mark.asyncio
async def test_execute_guest_spec(test_config):
    executor = ShellExecutor(test_config)

    with patch.object(executor, "execute_in_guest", new_callable=AsyncMock) as mock_exec_guest:
        mock_exec_guest.return_value = ShellResult(
            command="uptime", exit_code=0, stdout="up 5 days", stderr=""
        )

        res = await executor.execute_guest_spec("pve-node-01/100", "uptime")
        assert res.success
        assert res.stdout == "up 5 days"
        mock_exec_guest.assert_called_once_with("pve-node-01", 100, "uptime", timeout=30)
