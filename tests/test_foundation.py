"""Unit tests for Wheatley configuration and audit engine"""

from pathlib import Path
from wheatley.audit import AuditLogger
from wheatley.config import interpolate_env_vars, load_config


def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("TEST_FOO", "bar123")
    raw = "key: ${TEST_FOO}\ndefault: ${NON_EXISTENT:-my_default}"
    result = interpolate_env_vars(raw)
    assert "key: bar123" in result
    assert "default: my_default" in result


def test_audit_logger(tmp_path: Path):
    db_file = tmp_path / "test_audit.db"
    audit = AuditLogger(db_file)

    row_id = audit.log_event(
        event_type="TEST",
        source="unit_test",
        action="run_check",
        status="OK",
        target="pve-node-01",
        details={"ping": "success"},
    )
    assert row_id == 1

    logs = audit.get_recent_logs(limit=5)
    assert len(logs) == 1
    assert logs[0]["event_type"] == "TEST"
    assert logs[0]["action"] == "run_check"
    assert "ping" in logs[0]["details"]


def test_config_load_valid(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "123456:ABC-DEF")
    monkeypatch.setenv("TG_UID", "987654321")
    monkeypatch.setenv("NODE1_MON", "token-mon-secret")

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
nodes:
  - name: pve-node-01
    api_url: "https://192.168.1.10:8006"
    ssh:
      host: "192.168.1.10"
    tokens:
      monitor: "${NODE1_MON}"
chat:
  telegram:
    bot_token: "${TG_TOKEN}"
    allowed_user_ids:
      - ${TG_UID}
"""
    )

    config = load_config(cfg_file)
    assert len(config.nodes) == 1
    assert config.nodes[0].name == "pve-node-01"
    assert config.nodes[0].tokens.monitor == "token-mon-secret"
    assert config.chat.telegram is not None
    assert config.chat.telegram.bot_token == "123456:ABC-DEF"
    assert config.chat.telegram.allowed_user_ids == [987654321]
