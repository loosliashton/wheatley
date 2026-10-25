"""Tests for Wheatley Webhook Receiver"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wheatley.audit import AuditLogger
from wheatley.config import WebhookConfig
from wheatley.webhook import WebhookServer


@pytest.mark.asyncio
async def test_webhook_pve_flow(tmp_path):
    db_path = str(tmp_path / "test_webhook.db")
    audit = AuditLogger(db_path)

    alerts = []

    async def mock_alert_sink(msg: str, extra: dict):
        alerts.append((msg, extra))

    config = WebhookConfig(enabled=True, host="127.0.0.1", port=8088, secret_token="supersecret")
    server = WebhookServer(config, audit, alert_sink=mock_alert_sink)

    test_server = TestServer(server.app)
    client = TestClient(test_server)
    await client.start_server()

    try:
        # 1. Health endpoint (no auth needed)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

        # 2. Unauthorized request to /webhook/pve
        resp = await client.post("/webhook/pve", json={"title": "test"})
        assert resp.status == 401

        # 3. Authorized vzdump backup failure
        payload = {
            "title": "vzdump backup job failed",
            "message": "ERROR: Backup of VM 100 failed - storage full",
            "severity": "error",
            "data": {
                "type": "vzdump",
                "hostname": "cave",
                "id": "100",
            },
        }
        headers = {"Authorization": "Bearer supersecret"}
        resp = await client.post("/webhook/pve", json=payload, headers=headers)
        assert resp.status == 200
        assert len(alerts) == 1
        assert "PROXMOX ALERT" in alerts[0][0]
        assert "cave" in alerts[0][0]

        # 4. Authorized info event (no alert dispatched)
        info_payload = {
            "title": "vzdump backup succeeded",
            "message": "INFO: Backup of VM 100 finished successfully",
            "severity": "info",
            "data": {"type": "vzdump", "hostname": "cave"},
        }
        resp = await client.post("/webhook/pve", json=info_payload, headers=headers)
        assert resp.status == 200
        assert len(alerts) == 1  # Still 1, no new alert
    finally:
        await client.close()
