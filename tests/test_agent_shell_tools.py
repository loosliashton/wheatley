"""Unit tests for Agent run_host_command and run_guest_command tools"""

from unittest.mock import AsyncMock, MagicMock
import pytest

from wheatley.audit import AuditLogger
from wheatley.config import NodeConfig, NodeSshConfig, NodeTokensConfig, WheatleyConfig
from wheatley.llm.agent import LLMAgent
from wheatley.tools.shell import ShellResult


@pytest.fixture
def mock_agent(tmp_path):
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
    audit = AuditLogger(tmp_path / "audit.db")
    shell_mock = MagicMock()
    agent = LLMAgent(config=config, audit=audit, shell=shell_mock)
    return agent


@pytest.mark.asyncio
async def test_run_host_command_allowlisted(mock_agent):
    mock_agent.shell.execute_on_host = AsyncMock(
        return_value=ShellResult(
            command="uptime", exit_code=0, stdout="up 3 days", stderr="", timed_out=False
        )
    )

    result = await mock_agent._tool_run_host_command("cave", "uptime")
    assert result["status"] == "SUCCESS"
    assert result["stdout"] == "up 3 days"
    mock_agent.shell.execute_on_host.assert_called_once_with("cave", "uptime", timeout=20)


@pytest.mark.asyncio
async def test_run_host_command_blocked_produces_proposal(mock_agent):
    mock_agent.shell.execute_on_host = AsyncMock()

    result = await mock_agent._tool_run_host_command("cave", "reboot")
    assert result["status"] == "REQUIRES_APPROVAL"
    assert "proposal" in result
    assert result["proposal"]["target"] == "cave"
    assert result["proposal"]["command"] == "reboot"
    # Shell execute should NEVER have been called!
    mock_agent.shell.execute_on_host.assert_not_called()


@pytest.mark.asyncio
async def test_run_guest_command_allowlisted(mock_agent):
    mock_agent.shell.execute_in_guest = AsyncMock(
        return_value=ShellResult(
            command="docker ps", exit_code=0, stdout="CONTAINER ID   IMAGE", stderr="", timed_out=False
        )
    )

    result = await mock_agent._tool_run_guest_command("cave", 100, "docker ps")
    assert result["status"] == "SUCCESS"
    assert "CONTAINER ID" in result["stdout"]
    mock_agent.shell.execute_in_guest.assert_called_once_with("cave", 100, "docker ps", timeout=20)


@pytest.mark.asyncio
async def test_run_guest_command_blocked_produces_proposal(mock_agent):
    mock_agent.shell.execute_in_guest = AsyncMock()

    result = await mock_agent._tool_run_guest_command("cave", 100, "docker rm -f plex")
    assert result["status"] == "REQUIRES_APPROVAL"
    assert "proposal" in result
    assert result["proposal"]["target"] == "cave/100"
    mock_agent.shell.execute_in_guest.assert_not_called()
