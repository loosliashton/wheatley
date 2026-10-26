"""Unit tests for checks.yaml separation, loading, live reloading, and agent proposal flow"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram import Update, User, CallbackQuery
from telegram.ext import ContextTypes

from wheatley.approvals import ApprovalStore
from wheatley.audit import AuditLogger
from wheatley.chat.telegram import TelegramBot
from wheatley.checks.engine import CheckEngine
from wheatley.config import CheckConfig, TelegramConfig, WheatleyConfig, load_config
from wheatley.llm.agent import LLMAgent
from wheatley.llm.base import BaseLLMProvider, LLMResponse, ToolCall


CONFIG_YAML = """
nodes:
  - name: cave
    api_url: "https://127.0.0.1:8006"
    ssh:
      host: "127.0.0.1"
      port: 22
      user: "wheatley"
    tokens:
      monitor: "id=sec"

chat:
  telegram:
    bot_token: "123:ABC"
    allowed_user_ids: [12345]
"""

CHECKS_YAML = """
checks:
  - id: http-ping
    type: http
    enabled: true
    interval_seconds: 60
    params:
      url: "http://127.0.0.1:8080/health"

  - id: docker-containers
    type: docker
    enabled: true
    interval_seconds: 60
    params:
      guest: "cave/100"
      containers: "*"
      exclude: []
"""


def test_load_config_auto_detects_checks_yaml(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(CONFIG_YAML, encoding="utf-8")
    checks_file = tmp_path / "checks.yaml"
    checks_file.write_text(CHECKS_YAML, encoding="utf-8")

    cfg = load_config(cfg_file)
    assert len(cfg.checks) == 2
    assert cfg.checks[0].id == "http-ping"
    assert cfg.checks[1].id == "docker-containers"
    assert cfg.checks_file == str(checks_file)


def test_check_engine_reload_checks(tmp_path: Path):
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cfg = WheatleyConfig.model_validate({
        "chat": {"telegram": {"bot_token": "fake", "allowed_user_ids": [123]}},
        "checks": [
            {"id": "c1", "type": "http", "params": {"url": "http://127.0.0.1"}},
            {"id": "c2", "type": "http", "params": {"url": "http://127.0.0.2"}},
        ],
    })

    engine = CheckEngine(cfg, audit)
    assert len(engine.checks) == 2
    assert "c1" in engine.checks
    assert "c2" in engine.checks

    # Reload with c1 updated and c2 replaced by c3
    new_checks = [
        CheckConfig(id="c1", type="http", params={"url": "http://127.0.0.1/new"}),
        CheckConfig(id="c3", type="http", params={"url": "http://127.0.0.3"}),
    ]
    engine.reload_checks(new_checks)

    assert len(engine.checks) == 2
    assert "c1" in engine.checks
    assert "c3" in engine.checks
    assert "c2" not in engine.checks
    assert engine.checks["c1"].params["url"] == "http://127.0.0.1/new"


@pytest.mark.asyncio
async def test_agent_propose_check_config_update(tmp_path: Path):
    checks_file = tmp_path / "checks.yaml"
    checks_file.write_text(CHECKS_YAML, encoding="utf-8")

    audit = AuditLogger(str(tmp_path / "audit.db"))
    approvals = ApprovalStore(str(tmp_path / "audit.db"))

    cfg = WheatleyConfig.model_validate({
        "checks_file": str(checks_file),
        "chat": {"telegram": {"bot_token": "fake", "allowed_user_ids": [123]}},
    })

    mock_provider = MagicMock(spec=BaseLLMProvider)
    mock_provider.is_configured = True
    mock_provider.model_name = "mock"
    mock_provider.provider_name = "mock"
    mock_provider.generate_response = AsyncMock(
        side_effect=[
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_update",
                        name="propose_check_config_update",
                        arguments={
                            "check_id": "docker-containers",
                            "action": "add_exclude",
                            "values": ["seedboxapi", "floatplane-downloader"],
                            "summary": "Exclude stopped containers",
                        },
                    )
                ],
                input_tokens=100,
                output_tokens=50,
            ),
            LLMResponse(
                content="I have proposed updating checks.yaml.",
                tool_calls=[],
                input_tokens=120,
                output_tokens=20,
            ),
        ]
    )

    agent = LLMAgent(config=cfg, audit=audit, approvals=approvals, provider=mock_provider)
    await agent.chat("Ignore seedboxapi and floatplane-downloader")

    # Verify proposal created
    props = approvals.get_active_proposals()
    assert len(props) == 1
    assert props[0].proposal_type == "CONFIG_UPDATE"
    assert props[0].target == str(checks_file)

    payload = json.loads(props[0].command)
    assert "+        - seedboxapi" in payload["diff"] or "+        - \"seedboxapi\"" in payload["diff"]


@pytest.mark.asyncio
async def test_telegram_approves_config_update(tmp_path: Path):
    checks_file = tmp_path / "checks.yaml"
    checks_file.write_text(CHECKS_YAML, encoding="utf-8")

    audit = AuditLogger(str(tmp_path / "audit.db"))
    approvals = ApprovalStore(str(tmp_path / "audit.db"))

    cfg = WheatleyConfig.model_validate({
        "checks_file": str(checks_file),
        "chat": {"telegram": {"bot_token": "fake:token", "allowed_user_ids": [12345]}},
    })
    engine = CheckEngine(cfg, audit)

    mock_agent = MagicMock()
    mock_agent.is_configured = True
    mock_agent.chat = AsyncMock(return_value="Applied config update.")

    tg_cfg = TelegramConfig(bot_token="fake:token", allowed_user_ids=[12345])
    bot = TelegramBot(config=tg_cfg, audit=audit, approvals=approvals, check_engine=engine, llm_agent=mock_agent)
    bot._send_or_reply = AsyncMock()

    # Create config update proposal
    new_yaml = CHECKS_YAML.replace("exclude: []", 'exclude:\n        - "seedboxapi"')
    prop = approvals.create_proposal(
        proposal_type="CONFIG_UPDATE",
        target=str(checks_file),
        command=json.dumps({
            "file": str(checks_file),
            "check_id": "docker-containers",
            "new_yaml": new_yaml,
            "diff": "+ seedboxapi",
        }),
        summary="Add seedboxapi to exclude list",
    )

    query = MagicMock(spec=CallbackQuery)
    query.data = f"approve:{prop.id}"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = MagicMock()
    query.message.chat_id = 12345

    update = MagicMock(spec=Update)
    update.callback_query = query
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = 12345

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    await bot._handle_callback(update, context)

    # 1. Verify checks.yaml file was actually updated on disk
    updated_content = checks_file.read_text(encoding="utf-8")
    assert "seedboxapi" in updated_content

    # 2. Verify check engine was reloaded
    docker_check = engine.checks.get("docker-containers")
    assert docker_check is not None
    assert "seedboxapi" in docker_check.params["exclude"]

    # 3. Verify agent continuation message sent
    mock_agent.chat.assert_called_once()
    assert "Configuration update" in mock_agent.chat.call_args[0][0]
