"""Tests for Wheatley Gemini Integration & AI Reasoning Loop"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch
import pytest

from wheatley.audit import AuditLogger
from wheatley.budget import BudgetTracker
from wheatley.checks.engine import CheckEngine
from wheatley.config import BudgetConfig, WheatleyConfig
from wheatley.gemini import GeminiAgent


@pytest.fixture
def fake_gemini_env(tmp_path):
    db_path = str(tmp_path / "test_gemini.db")
    audit = AuditLogger(db_path)
    b_cfg = BudgetConfig(monthly_grant_usd=10.0, daily_cap_usd=2.0, model_flash="gemini-3.8-flash")
    budget = BudgetTracker(b_cfg, audit)

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

    agent = GeminiAgent(
        config=cfg,
        audit=audit,
        budget=budget,
        check_engine=check_engine,
        proxmox_clients=clients,
    )
    agent.api_key = "fake_test_key"
    agent.client = MagicMock()
    return agent, budget, mock_client


def test_system_prompt_builder(fake_gemini_env):
    agent, _, _ = fake_gemini_env
    prompt = agent.build_system_prompt()
    assert "Wheatley" in prompt
    assert "node-01" in prompt
    assert "plex" in prompt
    assert "node-01/110" in prompt
    assert "Interactive Approval Gate" in prompt


def test_tool_cluster_status(fake_gemini_env):
    agent, _, _ = fake_gemini_env
    res = agent._execute_tool("get_cluster_status", {})
    assert "nodes" in res
    assert "node-01" in res["nodes"]
    assert res["nodes"]["node-01"]["cpu_percent"] == 5.0
    assert "plex" in res["nodes"]["node-01"]["running_guests"]


def test_tool_guest_details(fake_gemini_env):
    agent, _, _ = fake_gemini_env
    res = agent._execute_tool("get_guest_details", {"node": "node-01", "vmid": 102})
    assert res["name"] == "plex"
    assert res["vmid"] == 102
    assert res["ips"] == ["10.0.0.102"]


def test_tool_self_protection(fake_gemini_env):
    agent, _, _ = fake_gemini_env
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
async def test_chat_unconfigured(fake_gemini_env):
    agent, _, _ = fake_gemini_env
    agent.client = None
    agent.api_key = ""
    res = await agent.chat("Why is Frigate slow?")
    assert "Gemini AI is unconfigured" in res


@pytest.mark.asyncio
async def test_chat_paused_by_budget(fake_gemini_env):
    agent, budget, _ = fake_gemini_env
    agent.client = MagicMock()
    # Force budget daily limit
    budget.config.daily_cap_usd = 0.001
    budget.record_usage("gemini-3.8-flash", 100_000, 100_000)

    res = await agent.chat("What's running?")
    assert "AI Reasoning Paused" in res


@pytest.mark.asyncio
async def test_chat_loop_with_function_calling(fake_gemini_env):
    agent, budget, _ = fake_gemini_env

    mock_client = MagicMock()
    agent.client = mock_client

    # Turn 1: Model calls get_cluster_status
    mock_func_call = MagicMock()
    mock_func_call.name = "get_cluster_status"
    mock_func_call.args = {}

    resp_turn1 = MagicMock()
    resp_turn1.function_calls = [mock_func_call]
    resp_turn1.candidates = [MagicMock()]
    resp_turn1.candidates[0].content = MagicMock()
    resp_turn1.usage_metadata = MagicMock()
    resp_turn1.usage_metadata.prompt_token_count = 1200
    resp_turn1.usage_metadata.candidates_token_count = 50

    # Turn 2: Model returns final text
    resp_turn2 = MagicMock()
    resp_turn2.function_calls = None
    resp_turn2.text = "Cluster node-01 is operating normally with CPU at 5% and 2 running guests."
    resp_turn2.usage_metadata = MagicMock()
    resp_turn2.usage_metadata.prompt_token_count = 1400
    resp_turn2.usage_metadata.candidates_token_count = 100

    mock_client.models.generate_content.side_effect = [resp_turn1, resp_turn2]

    answer = await agent.chat("How is the cluster doing?")
    assert "Cluster node-01 is operating normally" in answer
    assert budget.get_monthly_spend() > 0.0
    inp, outp = budget.get_monthly_tokens()
    assert inp == 2600
    assert outp == 150
