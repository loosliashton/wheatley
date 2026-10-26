"""Comprehensive Tests for Wheatley LLM-Agnostic Architecture & Usage Tracking"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from wheatley.audit import AuditLogger
from wheatley.checks.engine import CheckEngine
from wheatley.config import LLMConfig, WheatleyConfig
from wheatley.llm.agent import LLMAgent
from wheatley.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, ToolCall, ToolDefinition
from wheatley.llm.providers import get_llm_provider
from wheatley.llm.providers.gemini import GeminiProvider
from wheatley.llm.providers.openai_compatible import OpenAICompatibleProvider
from wheatley.llm.usage import UsageTracker


@pytest.fixture
def test_env(tmp_path):
    db_path = str(tmp_path / "test_llm.db")
    audit = AuditLogger(db_path)
    usage = UsageTracker(audit)

    cfg = WheatleyConfig.model_validate({
        "nodes": [
            {
                "name": "node-01",
                "api_url": "https://10.0.0.1:8006",
                "ssh": {"host": "10.0.0.1"},
                "tokens": {"monitor": "token=secret"},
            }
        ],
        "self_guest": "node-01/110",
        "llm": {
            "provider": "gemini",
            "model": "gemini-3.8-flash",
            "temperature": 0.2,
        },
        "chat": {"telegram": {"bot_token": "fake", "allowed_user_ids": [123]}},
        "checks": [
            {
                "id": "plex",
                "type": "http",
                "enabled": True,
                "interval_seconds": 60,
                "params": {"url": "http://127.0.0.1:32400/identity"},
            }
        ],
    })

    mock_client = MagicMock()
    mock_client.get_node_status.return_value = {
        "cpu": 0.05,
        "memory": {"used": 8 * 1024**3, "total": 32 * 1024**3},
        "uptime": 86400,
    }
    mock_client.get_guests.return_value = [
        {"vmid": 102, "name": "plex", "type": "lxc", "status": "running"},
        {"vmid": 110, "name": "wheatley", "type": "lxc", "status": "running"},
    ]
    mock_client.get_guest_config.return_value = {"onboot": 1, "memory": 2048, "cores": 2}
    mock_client.get_guest_ips.return_value = ["10.0.0.102"]

    clients = {"node-01": mock_client}
    check_engine = CheckEngine(cfg, audit)

    return cfg, audit, usage, clients, check_engine


# --- Config & Factory Tests ---

def test_llm_config_defaults():
    cfg = LLMConfig()
    assert cfg.provider == "gemini"
    assert cfg.model == "gemini-3.8-flash"
    assert cfg.temperature == 0.2
    assert cfg.max_tool_iterations == 10


def test_legacy_budget_block_adaptation():
    # If legacy config has budget block, it maps to llm
    data = {
        "chat": {"telegram": {"bot_token": "fake", "allowed_user_ids": [123]}},
        "budget": {
            "model_flash": "gemini-3.8-flash",
            "max_tool_iterations": 15,
        },
    }
    cfg = WheatleyConfig.model_validate(data)
    assert cfg.llm.provider == "gemini"
    assert cfg.llm.model == "gemini-3.8-flash"
    assert cfg.llm.max_tool_iterations == 15


def test_provider_factory():
    # Gemini
    p_gemini = get_llm_provider(LLMConfig(provider="gemini", model="gemini-3.8-flash"))
    assert isinstance(p_gemini, GeminiProvider)
    assert p_gemini.provider_name == "gemini"
    assert p_gemini.model_name == "gemini-3.8-flash"

    # Ollama
    p_ollama = get_llm_provider(LLMConfig(provider="ollama", model="llama3.2"))
    assert isinstance(p_ollama, OpenAICompatibleProvider)
    assert p_ollama.provider_name == "ollama"
    assert p_ollama.model_name == "llama3.2"
    assert p_ollama._api_base == "http://localhost:11434/v1"
    assert p_ollama.is_configured is True

    # OpenAI
    p_openai = get_llm_provider(LLMConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test"))
    assert isinstance(p_openai, OpenAICompatibleProvider)
    assert p_openai.provider_name == "openai"
    assert p_openai._api_base == "https://api.openai.com/v1"
    assert p_openai.is_configured is True

    # Groq
    p_groq = get_llm_provider(LLMConfig(provider="groq", model="llama-3.3-70b-versatile", api_key="gsk-test"))
    assert isinstance(p_groq, OpenAICompatibleProvider)
    assert p_groq.provider_name == "groq"
    assert p_groq._api_base == "https://api.groq.com/openai/v1"


# --- Usage Tracker Tests (No Billing Caps/Warnings) ---

def test_usage_tracker_telemetry(test_env):
    _, audit, usage, _, _ = test_env
    assert usage.get_daily_tokens() == (0, 0)
    assert usage.get_monthly_tokens() == (0, 0)
    assert usage.get_total_requests() == 0

    # Record usage
    usage.record_usage(
        model="gemini-3.8-flash",
        input_tokens=1500,
        output_tokens=300,
        context="test_chat",
    )

    day_in, day_out = usage.get_daily_tokens()
    assert day_in == 1500
    assert day_out == 300

    month_in, month_out = usage.get_monthly_tokens()
    assert month_in == 1500
    assert month_out == 300
    assert usage.get_total_requests() == 1

    summary = usage.format_usage_markdown(provider="gemini", model="gemini-3.8-flash")
    assert "LLM Usage & Activity" in summary
    assert "gemini-3.8-flash" in summary
    assert "1,800" in summary
    # Verify no warning thresholds or pause caps
    assert "WARNING" not in summary
    assert "PAUSED" not in summary
    assert "no artificial billing caps" in summary


# --- OpenAICompatibleProvider Tests ---

def test_openai_compatible_message_and_tool_conversion():
    provider = OpenAICompatibleProvider(
        model="llama3.2",
        provider_name="ollama",
    )

    tools = [
        ToolDefinition(
            name="get_cluster_status",
            description="Query cluster status",
            parameters={"type": "object", "properties": {}},
        )
    ]
    converted_tools = provider._convert_tools(tools)
    assert len(converted_tools) == 1
    assert converted_tools[0]["type"] == "function"
    assert converted_tools[0]["function"]["name"] == "get_cluster_status"

    messages = [
        LLMMessage(role="user", content="Hello"),
        LLMMessage(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="call_1", name="get_cluster_status", arguments={})],
        ),
        LLMMessage(
            role="tool",
            tool_call_id="call_1",
            name="get_cluster_status",
            content={"nodes": {"node-01": "OK"}},
        ),
    ]
    converted_msgs = provider._convert_messages(messages, system_instruction="You are Wheatley.")
    assert converted_msgs[0] == {"role": "system", "content": "You are Wheatley."}
    assert converted_msgs[1] == {"role": "user", "content": "Hello"}
    assert converted_msgs[2]["role"] == "assistant"
    assert converted_msgs[2]["tool_calls"][0]["id"] == "call_1"
    assert converted_msgs[3]["role"] == "tool"
    assert converted_msgs[3]["tool_call_id"] == "call_1"
    assert "node-01" in converted_msgs[3]["content"]


@pytest.mark.asyncio
async def test_openai_compatible_generate_response():
    provider = OpenAICompatibleProvider(
        model="llama3.2",
        provider_name="ollama",
        api_base="http://localhost:11434/v1",
    )

    fake_response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "All nodes healthy.",
                    "tool_calls": [],
                }
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response_data

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await provider.generate_response(
            messages=[LLMMessage(role="user", content="Status?")],
            tools=[],
            system_instruction="System prompt",
        )
        assert res.content == "All nodes healthy."
        assert res.input_tokens == 50
        assert res.output_tokens == 10
        assert res.tool_calls == []


# --- LLMAgent Tool & Reasoning Loop Tests ---

def test_system_prompt_builder(test_env):
    cfg, audit, usage, clients, check_engine = test_env
    agent = LLMAgent(cfg, audit, usage=usage, check_engine=check_engine, proxmox_clients=clients)
    prompt = agent.build_system_prompt()
    assert "Wheatley" in prompt
    assert "node-01" in prompt
    assert "plex" in prompt
    assert "node-01/110" in prompt
    assert "Interactive Approval Gate" in prompt


def test_tool_cluster_status(test_env):
    cfg, audit, usage, clients, check_engine = test_env
    agent = LLMAgent(cfg, audit, usage=usage, check_engine=check_engine, proxmox_clients=clients)
    res = agent._execute_tool("get_cluster_status", {})
    assert "nodes" in res
    assert "node-01" in res["nodes"]
    assert res["nodes"]["node-01"]["cpu_percent"] == 5.0
    assert "plex" in res["nodes"]["node-01"]["running_guests"]
    assert any(g["name"] == "plex" and g["type"] == "lxc" and g["vmid"] == 102 for g in res["nodes"]["node-01"]["guests"])


def test_tool_guest_details(test_env):
    cfg, audit, usage, clients, check_engine = test_env
    agent = LLMAgent(cfg, audit, usage=usage, check_engine=check_engine, proxmox_clients=clients)
    res = agent._execute_tool("get_guest_details", {"node": "node-01", "vmid": 102})
    assert res["name"] == "plex"
    assert res["vmid"] == 102
    assert res["ips"] == ["10.0.0.102"]


def test_tool_self_protection(test_env):
    cfg, audit, usage, clients, check_engine = test_env
    agent = LLMAgent(cfg, audit, usage=usage, check_engine=check_engine, proxmox_clients=clients)
    # Target self (node-01/110)
    res = agent._execute_tool("propose_action", {
        "target": "node-01/110",
        "command": "pct stop 110",
        "summary": "Stop Wheatley container",
        "risk": "HIGH",
    })
    assert res["status"] == "REFUSED"
    assert "Self-protection triggered" in res["reason"]

    # Target regular guest
    res_allowed = agent._execute_tool("propose_action", {
        "target": "node-01/102",
        "command": "pct restart 102",
        "summary": "Restart Plex",
        "risk": "MEDIUM",
    })
    assert res_allowed["status"] == "PROPOSED"


@pytest.mark.asyncio
async def test_chat_unconfigured(test_env):
    cfg, audit, usage, clients, check_engine = test_env
    mock_provider = MagicMock(spec=BaseLLMProvider)
    mock_provider.is_configured = False
    mock_provider.provider_name = "gemini"

    agent = LLMAgent(cfg, audit, provider=mock_provider, usage=usage)
    res = await agent.chat("Why is Frigate down?")
    assert "unconfigured" in res


@pytest.mark.asyncio
async def test_chat_multi_turn_with_function_calling(test_env):
    cfg, audit, usage, clients, check_engine = test_env

    mock_provider = MagicMock(spec=BaseLLMProvider)
    mock_provider.is_configured = True
    mock_provider.provider_name = "mock_provider"
    mock_provider.model_name = "mock-model"

    # Turn 1: Model requests get_cluster_status
    resp_turn1 = LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name="get_cluster_status", arguments={})],
        input_tokens=500,
        output_tokens=30,
    )

    # Turn 2: Model returns final diagnostic answer
    resp_turn2 = LLMResponse(
        content="Node node-01 is operating with CPU at 5.0% and Plex is running.",
        tool_calls=[],
        input_tokens=600,
        output_tokens=80,
    )

    mock_provider.generate_response = AsyncMock(side_effect=[resp_turn1, resp_turn2])

    agent = LLMAgent(
        cfg,
        audit,
        provider=mock_provider,
        usage=usage,
        check_engine=check_engine,
        proxmox_clients=clients,
    )

    answer = await agent.chat("How is node-01?")
    assert "Node node-01 is operating with CPU at 5.0%" in answer

    # Verify usage was recorded without any billing pause
    month_in, month_out = usage.get_monthly_tokens()
    assert month_in == 1100
    assert month_out == 110


@pytest.mark.asyncio
async def test_no_billing_caps_or_pauses(test_env):
    """Verify that even huge token consumption does NOT pause or block LLM calls."""
    cfg, audit, usage, clients, check_engine = test_env

    mock_provider = MagicMock(spec=BaseLLMProvider)
    mock_provider.is_configured = True
    mock_provider.provider_name = "mock_provider"
    mock_provider.model_name = "mock-model"
    mock_provider.generate_response = AsyncMock(
        return_value=LLMResponse(content="Still answering!", tool_calls=[], input_tokens=500, output_tokens=50)
    )

    agent = LLMAgent(cfg, audit, provider=mock_provider, usage=usage)

    # Record 50 million tokens
    usage.record_usage("mock-model", 40_000_000, 10_000_000)

    # Chat must proceed normally without any budget pause
    reply = await agent.chat("Are you paused?")
    assert "Still answering!" in reply


# --- Conversation Memory & Tool Truncation Tests ---

def test_conversation_store_persistence(tmp_path):
    from wheatley.llm.memory import ConversationStore
    db_file = str(tmp_path / "memory_test.db")
    store = ConversationStore(db_file)

    store.add_message("chat_1", "user", "Hello Wheatley")
    store.add_message("chat_1", "assistant", "Greetings Commander")
    store.add_message("chat_2", "user", "Different chat question")

    h1 = store.get_history("chat_1", max_turns=5)
    assert len(h1) == 2
    assert h1[0].role == "user"
    assert h1[0].content == "Hello Wheatley"
    assert h1[1].role == "assistant"
    assert h1[1].content == "Greetings Commander"

    h2 = store.get_history("chat_2", max_turns=5)
    assert len(h2) == 1
    assert h2[0].content == "Different chat question"

    # Reset chat_1
    deleted = store.clear_history("chat_1")
    assert deleted == 2
    assert store.get_history("chat_1") == []
    # chat_2 remains untouched
    assert len(store.get_history("chat_2")) == 1


@pytest.mark.asyncio
async def test_chat_multi_turn_context_retention(test_env, tmp_path):
    from wheatley.llm.memory import ConversationStore
    cfg, audit, usage, clients, check_engine = test_env
    mem_db = str(tmp_path / "agent_memory.db")
    store = ConversationStore(mem_db)

    mock_provider = MagicMock(spec=BaseLLMProvider)
    mock_provider.is_configured = True
    mock_provider.provider_name = "mock"
    mock_provider.model_name = "mock-model"

    # Capture messages sent to provider
    captured_messages = []
    async def mock_generate(messages, tools, system_instruction):
        captured_messages.append(list(messages))
        return LLMResponse(content=f"Answer to: {messages[-1].content}")

    mock_provider.generate_response = AsyncMock(side_effect=mock_generate)

    agent = LLMAgent(cfg, audit, provider=mock_provider, usage=usage, memory=store)

    # Turn 1
    reply1 = await agent.chat("Remember: my secret code is 42", chat_id="user_123")
    assert "Answer to: Remember: my secret code is 42" in reply1
    assert len(captured_messages[0]) == 1
    assert captured_messages[0][0].content == "Remember: my secret code is 42"

    # Turn 2: should include turn 1 history!
    await agent.chat("What is my code?", chat_id="user_123")
    assert len(captured_messages[1]) == 3
    assert captured_messages[1][0].role == "user"
    assert captured_messages[1][0].content == "Remember: my secret code is 42"
    assert captured_messages[1][1].role == "assistant"
    assert captured_messages[1][2].role == "user"
    assert captured_messages[1][2].content == "What is my code?"

    # Reset chat
    del_count = agent.reset_conversation("user_123")
    assert del_count == 4  # 2 user + 2 assistant messages

    # Turn 3 after reset: should start fresh without history
    await agent.chat("Hello fresh turn", chat_id="user_123")
    assert len(captured_messages[2]) == 1
    assert captured_messages[2][0].content == "Hello fresh turn"


def test_tool_result_truncation(test_env):
    cfg, audit, usage, clients, check_engine = test_env
    cfg.llm.max_tool_result_chars = 100
    agent = LLMAgent(cfg, audit, usage=usage)

    # 1. Truncate long string
    long_str = "x" * 250
    truncated_str = agent._truncate_tool_result(long_str)
    assert len(truncated_str) < 250
    assert "truncated" in truncated_str

    # 2. Truncate large dict
    large_dict = {"data": "y" * 300, "meta": "info"}
    truncated_dict = agent._truncate_tool_result(large_dict)
    assert "truncated" in str(truncated_dict)


@pytest.mark.asyncio
async def test_investigate_alert(test_env):
    cfg, audit, usage, clients, check_engine = test_env
    mock_provider = MagicMock(spec=BaseLLMProvider)
    mock_provider.is_configured = True
    mock_provider.provider_name = "mock"
    mock_provider.model_name = "mock-model"
    mock_provider.generate_response = AsyncMock(
        return_value=LLMResponse(
            content="Probable cause: Out of memory on Plex LXC.",
            tool_calls=[],
            input_tokens=800,
            output_tokens=40,
        )
    )

    agent = LLMAgent(cfg, audit, provider=mock_provider, usage=usage)
    diag = await agent.investigate_alert("plex", "HTTP 503 Service Unavailable", chat_id="alert_test")
    assert "Out of memory" in diag


@pytest.mark.asyncio
async def test_llm_footer_distinguishes_ai_messages(test_env):
    cfg, audit, usage, clients, check_engine = test_env
    mock_provider = MagicMock(spec=BaseLLMProvider)
    mock_provider.is_configured = True
    mock_provider.provider_name = "gemini"
    mock_provider.model_name = "gemini-3.8-flash"

    # Response with 0 tool calls
    mock_provider.generate_response = AsyncMock(
        return_value=LLMResponse(
            content="Cluster is operating normally.",
            tool_calls=[],
        )
    )
    agent = LLMAgent(cfg, audit, provider=mock_provider, usage=usage)
    reply0 = await agent.chat("Status?")
    assert "Cluster is operating normally." in reply0
    assert "_🧠 gemini-3.8-flash_" in reply0

    # Response with tool calls
    resp1 = LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="t1", name="get_check_states", arguments={})],
    )
    resp2 = LLMResponse(
        content="All checks look good.",
        tool_calls=[],
    )
    mock_provider.generate_response = AsyncMock(side_effect=[resp1, resp2])
    reply1 = await agent.chat("Run checks", chat_id="tools_test")
    assert "All checks look good." in reply1
    assert "_🧠 gemini-3.8-flash · 1 tool call_" in reply1


