"""Unit tests for Agent run_host_command and run_guest_command tools"""

from unittest.mock import AsyncMock, MagicMock
import pytest

from wheatley.audit import AuditLogger
from wheatley.config import NodeConfig, NodeSshConfig, NodeTokensConfig, WheatleyConfig
from wheatley.llm.agent import LLMAgent, CommandExecutionHalted
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

    with pytest.raises(CommandExecutionHalted) as excinfo:
        await mock_agent._tool_run_host_command("cave", "reboot")
    prop = excinfo.value.proposal
    assert prop.target == "cave"
    assert prop.command == "reboot"
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

    with pytest.raises(CommandExecutionHalted) as excinfo:
        await mock_agent._tool_run_guest_command("cave", 100, "docker rm -f plex")
    prop = excinfo.value.proposal
    assert prop.target == "cave/100"
    mock_agent.shell.execute_in_guest.assert_not_called()


@pytest.mark.asyncio
async def test_dynamic_target_allowlist_permits_command(mock_agent):
    mock_agent.shell.execute_in_guest = AsyncMock(
        return_value=ShellResult(
            command="snapraid status", exit_code=0, stdout="Everything OK", stderr="", timed_out=False
        )
    )

    # 1. First attempt on cave/101 is blocked because snapraid is not in DEFAULT_ALLOWLIST
    with pytest.raises(CommandExecutionHalted) as excinfo:
        await mock_agent._tool_run_guest_command("cave", 101, "snapraid status")
    prop_id = excinfo.value.proposal.id

    # 2. User approves with ALLOW_ALWAYS on cave/101
    ok, msg = mock_agent.approvals.resolve_proposal(prop_id, "ALLOW_ALWAYS", user_id=123)
    assert ok is True

    # 3. Second attempt on cave/101 now executes!
    res2 = await mock_agent._tool_run_guest_command("cave", 101, "snapraid status")
    assert res2["status"] == "SUCCESS"
    assert res2["stdout"] == "Everything OK"

    # 4. Same command on cave/102 is still blocked (target scoped)
    with pytest.raises(CommandExecutionHalted):
        await mock_agent._tool_run_guest_command("cave", 102, "snapraid status")


@pytest.mark.asyncio
async def test_chat_halts_on_unallowed_command(mock_agent):
    from wheatley.llm.base import BaseLLMProvider, LLMResponse, ToolCall

    mock_provider = MagicMock(spec=BaseLLMProvider)
    mock_provider.is_configured = True
    mock_provider.model_name = "mock"
    mock_provider.provider_name = "mock"
    mock_provider.generate_response = AsyncMock(
        return_value=LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c1", name="run_guest_command", arguments={"node": "cave", "vmid": 101, "command": "snapraid status"})],
            input_tokens=50,
            output_tokens=20,
        )
    )
    mock_agent.provider = mock_provider

    response = await mock_agent.chat("Check snapraid", chat_id="t1")
    assert "I need permission to execute `snapraid status` on `cave/101`" in response
    active_props = mock_agent.approvals.get_active_proposals()
    assert any(p.command == "snapraid status" for p in active_props)


@pytest.mark.asyncio
async def test_turn_limit_graceful_synthesis(mock_agent):
    from wheatley.llm.base import LLMResponse, ToolCall

    mock_agent.config.llm.max_tool_iterations = 2
    mock_agent.config.llm.provider = "mock"

    call_count = 0

    async def mock_generate(messages, tools, system_instruction):
        nonlocal call_count
        call_count += 1
        if tools:  # inside loop
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id=f"c_{call_count}", name="get_recent_audit_logs", arguments={"limit": 1})],
                input_tokens=100,
                output_tokens=50,
            )
        else:  # synthesis call with tools=[]
            return LLMResponse(
                content="Synthesized final report: telemetry is normal and cluster is healthy.",
                tool_calls=[],
                input_tokens=200,
                output_tokens=100,
            )

    from wheatley.llm.base import BaseLLMProvider
    mock_provider = MagicMock(spec=BaseLLMProvider)
    mock_provider.is_configured = True
    mock_provider.model_name = "mock-model"
    mock_provider.provider_name = "mock"
    mock_provider.generate_response = AsyncMock(side_effect=mock_generate)
    mock_agent.provider = mock_provider

    answer = await mock_agent.chat("System status overview?")
    assert "Synthesized final report" in answer
    assert "Diagnostic tool budget reached; synthesized from gathered observations" in answer
    assert call_count == 3  # 2 iteration loop calls + 1 synthesis call
