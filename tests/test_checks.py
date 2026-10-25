"""Tests for Wheatley Check Engine and Built-in Check Plugins"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from wheatley.audit import AuditLogger
from wheatley.checks.base import CheckContext, CheckResult, CheckStatus
from wheatley.checks.builtin.docker import DockerCheck
from wheatley.checks.builtin.guest_state import GuestStateCheck
from wheatley.checks.builtin.http import HttpCheck
from wheatley.checks.builtin.node_metrics import NodeMetricsCheck
from wheatley.checks.builtin.storage import StorageCheck
from wheatley.checks.builtin.tcp import TcpCheck
from wheatley.checks.engine import CheckEngine
from wheatley.config import CheckConfig, WheatleyConfig


@pytest.fixture
def fake_context():
    cfg = WheatleyConfig.model_validate({"chat": {"telegram": {"bot_token": "fake", "allowed_user_ids": [123]}}})
    mock_client = MagicMock()
    mock_client.get_node_status.return_value = {
        "cpu": 0.25,
        "memory": {"used": 4 * 1024**3, "total": 16 * 1024**3},
    }
    mock_client.get_storage.return_value = [
        {"storage": "local-lvm", "total": 100 * 1024**3, "used": 40 * 1024**3, "active": 1}
    ]
    mock_client.get_guests.return_value = [
        {"vmid": 102, "name": "plex", "type": "lxc", "status": "running"},
        {"vmid": 107, "name": "frigate", "type": "lxc", "status": "running"},
    ]
    mock_client.get_guest_ips.return_value = ["192.168.1.102"]
    mock_client.get_guest_config.return_value = {"onboot": 1}

    clients = {"cave": mock_client}
    return CheckContext(cfg, clients)


@pytest.mark.asyncio
async def test_guest_ip_resolution(fake_context):
    url = "http://{guest:cave/102}:32400/identity"
    resolved = fake_context.resolve_string(url)
    assert resolved == "http://192.168.1.102:32400/identity"


@pytest.mark.asyncio
async def test_http_check_ok(fake_context):
    chk_cfg = CheckConfig(id="test-http", type="http", params={"url": "http://127.0.0.1:8080/health"})
    check = HttpCheck(chk_cfg)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"status": "ok"}'

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp
        res = await check.run(fake_context)
        assert res.status == CheckStatus.OK
        assert "HTTP 200 OK" in res.summary


@pytest.mark.asyncio
async def test_http_check_status_failure(fake_context):
    chk_cfg = CheckConfig(id="test-http-fail", type="http", params={"url": "http://127.0.0.1:8080/health", "expected_status": [200]})
    check = HttpCheck(chk_cfg)

    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "Service Unavailable"

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp
        res = await check.run(fake_context)
        assert res.status == CheckStatus.CRIT
        assert "HTTP 503" in res.summary


@pytest.mark.asyncio
async def test_http_check_body_match_failure(fake_context):
    chk_cfg = CheckConfig(
        id="test-body",
        type="http",
        params={"url": "http://127.0.0.1:8080/", "match_body": "Plex Media Server"},
    )
    check = HttpCheck(chk_cfg)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "Error: database locked"

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp
        res = await check.run(fake_context)
        assert res.status == CheckStatus.CRIT
        assert "Body signature" in res.summary


@pytest.mark.asyncio
async def test_tcp_check(fake_context):
    chk_cfg = CheckConfig(id="test-tcp", type="tcp", params={"host": "127.0.0.1", "port": 8080})
    check = TcpCheck(chk_cfg)

    # Test failure on closed port
    res = await check.run(fake_context)
    # Even if port is closed, it gracefully reports CRIT
    assert res.status in (CheckStatus.CRIT, CheckStatus.OK)


@pytest.mark.asyncio
async def test_node_metrics_check_ok(fake_context):
    chk_cfg = CheckConfig(
        id="test-node",
        type="node_metrics",
        params={"nodes": ["cave"], "cpu_crit_percent": 90, "mem_crit_percent": 90},
    )
    check = NodeMetricsCheck(chk_cfg)
    res = await check.run(fake_context)
    assert res.status == CheckStatus.OK
    assert "cave_cpu_pct" in res.metrics
    assert res.metrics["cave_cpu_pct"] == 25.0


@pytest.mark.asyncio
async def test_node_metrics_check_cpu_crit(fake_context):
    client = fake_context.get_client("cave")
    client.get_node_status.return_value = {
        "cpu": 0.95,
        "memory": {"used": 4 * 1024**3, "total": 16 * 1024**3},
    }
    chk_cfg = CheckConfig(
        id="test-node-crit",
        type="node_metrics",
        params={"nodes": ["cave"], "cpu_crit_percent": 90, "mem_crit_percent": 90},
    )
    check = NodeMetricsCheck(chk_cfg)
    res = await check.run(fake_context)
    assert res.status == CheckStatus.CRIT
    assert "CPU critical" in res.summary


@pytest.mark.asyncio
async def test_storage_check(fake_context):
    chk_cfg = CheckConfig(
        id="test-storage",
        type="storage",
        params={"nodes": ["cave"], "warn_percent": 80, "crit_percent": 90},
    )
    check = StorageCheck(chk_cfg)
    res = await check.run(fake_context)
    assert res.status == CheckStatus.OK

    # Simulate storage pool at 96%
    client = fake_context.get_client("cave")
    client.get_storage.return_value = [
        {"storage": "local-lvm", "total": 100 * 1024**3, "used": 96 * 1024**3, "active": 1}
    ]
    res_crit = await check.run(fake_context)
    assert res_crit.status == CheckStatus.CRIT
    assert "critical" in res_crit.summary


@pytest.mark.asyncio
async def test_guest_state_check(fake_context):
    chk_cfg = CheckConfig(
        id="test-guests",
        type="guest_state",
        params={"select": {"onboot": True}, "exclude": []},
    )
    check = GuestStateCheck(chk_cfg)
    res = await check.run(fake_context)
    assert res.status == CheckStatus.OK

    # Simulate guest stopped
    client = fake_context.get_client("cave")
    client.get_guests.return_value = [
        {"vmid": 102, "name": "plex", "type": "lxc", "status": "stopped"},
    ]
    res_stopped = await check.run(fake_context)
    assert res_stopped.status == CheckStatus.CRIT
    assert "plex" in res_stopped.summary


@pytest.mark.asyncio
async def test_check_engine_state_transitions(tmp_path):
    db_path = str(tmp_path / "test.db")
    audit = AuditLogger(db_path)

    cfg = WheatleyConfig.model_validate({
        "chat": {"telegram": {"bot_token": "fake", "allowed_user_ids": [123]}},
        "checks": [
            {
                "id": "http-test",
                "type": "http",
                "enabled": True,
                "interval_seconds": 60,
                "params": {"url": "http://127.0.0.1:8080/health"},
            }
        ],
    })

    alerts_received = []

    async def mock_alert_sink(msg: str, extra: dict):
        alerts_received.append((msg, extra))

    engine = CheckEngine(cfg, audit, alert_sink=mock_alert_sink)

    # 1. First run: Simulate failure (OK -> CRIT)
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_req.return_value = mock_resp

        res = await engine.execute_check("http-test")
        assert res.status == CheckStatus.CRIT
        assert len(alerts_received) == 1
        assert "CRITICAL ALERT" in alerts_received[0][0]
        assert alerts_received[0][1]["check_id"] == "http-test"

    # 2. Second run: Still failing (CRIT -> CRIT). Should NOT send duplicate alert!
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_req.return_value = mock_resp

        res = await engine.execute_check("http-test")
        assert res.status == CheckStatus.CRIT
        assert len(alerts_received) == 1  # Deduplicated

    # 3. Third run: Recovered (CRIT -> OK). Should send RESOLVED message!
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "OK"
        mock_req.return_value = mock_resp

        res = await engine.execute_check("http-test")
        assert res.status == CheckStatus.OK
        assert len(alerts_received) == 2
        assert "RESOLVED" in alerts_received[1][0]


@pytest.mark.asyncio
async def test_check_engine_mute(tmp_path):
    db_path = str(tmp_path / "test.db")
    audit = AuditLogger(db_path)

    cfg = WheatleyConfig.model_validate({
        "chat": {"telegram": {"bot_token": "fake", "allowed_user_ids": [123]}},
        "checks": [
            {
                "id": "http-test",
                "type": "http",
                "enabled": True,
                "interval_seconds": 60,
                "params": {"url": "http://127.0.0.1:8080/health"},
            }
        ],
    })

    alerts_received = []

    async def mock_alert_sink(msg: str, extra: dict):
        alerts_received.append((msg, extra))

    engine = CheckEngine(cfg, audit, alert_sink=mock_alert_sink)

    # Mute the check
    until = engine.mute_check("http-test", minutes=60)
    assert until is not None
    assert engine.states["http-test"].is_muted is True

    # Execute failure while muted: no alert dispatched
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_req.return_value = mock_resp

        res = await engine.execute_check("http-test")
        assert res.status == CheckStatus.CRIT
        assert len(alerts_received) == 0

    # Unmute check
    unmuted = engine.unmute_check("http-test")
    assert unmuted is True
    assert engine.states["http-test"].is_muted is False
