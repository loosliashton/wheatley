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


@pytest.mark.asyncio
async def test_execute_in_guest_caches_lxc_type(test_config):
    executor = ShellExecutor(test_config)

    vm_fail = ShellResult(
        command="uptime",
        exit_code=2,
        stdout="",
        stderr="Configuration file 'nodes/pve-node-01/qemu-server/102.conf' does not exist",
    )
    lxc_ok = ShellResult(command="uptime", exit_code=0, stdout="up 10 days", stderr="")

    with patch.object(executor, "execute_in_vm", new_callable=AsyncMock, return_value=vm_fail) as mock_vm, \
         patch.object(executor, "execute_in_lxc", new_callable=AsyncMock, return_value=lxc_ok) as mock_lxc:
        # First call probes VM, falls back to LXC, and caches "lxc"
        res1 = await executor.execute_in_guest("pve-node-01", 102, "uptime")
        assert res1.success
        assert mock_vm.call_count == 1
        assert mock_lxc.call_count == 1
        assert executor._guest_type_cache.get("pve-node-01/102") == "lxc"

        # Second call goes directly to LXC without calling execute_in_vm
        res2 = await executor.execute_in_guest("pve-node-01", 102, "df -h")
        assert res2.success
        assert mock_vm.call_count == 1
        assert mock_lxc.call_count == 2


@pytest.mark.asyncio
async def test_ssh_tofu_known_hosts_pinning(test_config, tmp_path):
    import asyncssh

    kh_file = tmp_path / "known_hosts"
    test_config.nodes[0].ssh.known_hosts_path = str(kh_file)
    executor = ShellExecutor(test_config)

    generated_key = asyncssh.generate_private_key("ssh-ed25519")

    mock_run_res = MagicMock()
    mock_run_res.exit_status = 0
    mock_run_res.stdout = "up 1 day\n"
    mock_run_res.stderr = ""

    mock_conn1 = MagicMock()
    mock_conn1.is_closed.return_value = False
    mock_conn1.run = AsyncMock(return_value=mock_run_res)
    mock_conn1.get_server_host_key.return_value = generated_key

    with patch("asyncssh.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn1

        # 1. First connection: known_hosts does not exist yet -> connects with known_hosts=None and pins key
        res1 = await executor.execute_on_host("pve-node-01", "uptime")
        assert res1.success
        assert mock_connect.call_args.kwargs["known_hosts"] is None
        assert kh_file.is_file()
        assert "192.168.1.111 ssh-ed25519" in kh_file.read_text()

        # Simulate reconnect: second connection loads and passes pinned SSHKnownHosts object
        mock_conn1.is_closed.return_value = True
        mock_conn2 = MagicMock()
        mock_conn2.is_closed.return_value = False
        mock_conn2.run = AsyncMock(return_value=mock_run_res)
        mock_connect.return_value = mock_conn2

        res2 = await executor.execute_on_host("pve-node-01", "uptime")
        assert res2.success
        assert mock_connect.call_args.kwargs["known_hosts"] is not None


def test_wheatley_shell_inline_copy_matches_file():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    shell_content = (repo_root / "install" / "wheatley-shell").read_text(encoding="utf-8").rstrip("\n")
    host_setup = (repo_root / "install" / "host-setup.sh").read_text(encoding="utf-8")
    assert shell_content in host_setup, "Inline wheatley-shell fallback in host-setup.sh is out of sync with install/wheatley-shell"
