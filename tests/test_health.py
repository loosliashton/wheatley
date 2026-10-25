"""Tests for the self-health endpoint and push heartbeat."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from wheatley.audit import AuditLogger
from wheatley.config import HealthConfig, WebhookConfig
from wheatley.health import HealthMonitor
from wheatley.webhook import WebhookServer


def _monitor(probes, **cfg) -> HealthMonitor:
    m = HealthMonitor(HealthConfig(**cfg))
    for name, probe in probes.items():
        m.register(name, probe)
    return m


def test_snapshot_healthy_and_degraded():
    m = _monitor({"a": lambda: {"ok": True}, "b": lambda: {"ok": True}})
    assert m.snapshot()["status"] == "ok"

    m.register("c", lambda: {"ok": False})
    report = m.snapshot()
    assert report["status"] == "degraded"
    assert report["components"]["c"]["ok"] is False


def test_probe_exception_counts_as_unhealthy():
    def boom():
        raise RuntimeError("secret detail 192.168.1.5")

    report = _monitor({"x": boom}).snapshot()
    assert report["status"] == "degraded"
    # Only the exception type is exposed, never the message
    assert report["components"]["x"] == {"ok": False, "error": "RuntimeError"}


@pytest.mark.asyncio
async def test_health_endpoint_status_codes(tmp_path):
    audit = AuditLogger(str(tmp_path / "h.db"))
    state = {"ok": True}
    monitor = _monitor({"engine": lambda: {"ok": state["ok"]}})
    server = WebhookServer(WebhookConfig(secret_token="s"), audit, health_monitor=monitor)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/health")
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"

        state["ok"] = False
        resp = await client.get("/health")
        assert resp.status == 503
        assert (await resp.json())["status"] == "degraded"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_health_only_listener_has_no_webhook_routes(tmp_path):
    audit = AuditLogger(str(tmp_path / "h.db"))
    server = WebhookServer(WebhookConfig(), audit, health_monitor=_monitor({}), serve_webhooks=False)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        assert (await client.get("/health")).status == 200
        assert (await client.post("/webhook/pve", json={})).status in (404, 405)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_push_only_when_healthy():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    state = {"ok": True}
    m = _monitor({"engine": lambda: {"ok": state["ok"]}}, push_url="https://kuma.example/api/push/TOKEN")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await m.push_once(client) is True
        state["ok"] = False
        assert await m.push_once(client) is False

    assert calls == ["https://kuma.example/api/push/TOKEN"]


@pytest.mark.asyncio
async def test_push_disabled_when_url_blank():
    m = _monitor({"engine": lambda: {"ok": True}}, push_url="")
    assert m.push_enabled is False
    assert await m.push_once() is False


def test_engine_health_detects_stall():
    from wheatley.checks.engine import CheckEngine
    from wheatley.config import WheatleyConfig

    cfg = WheatleyConfig(
        chat={"telegram": None},
        checks=[{"id": "t", "type": "tcp", "interval_seconds": 60, "params": {"host": "127.0.0.1", "port": 1}}],
    )
    engine = CheckEngine(cfg, audit=None)  # type: ignore[arg-type]

    class _Sched:
        running = True

    engine.scheduler = _Sched()  # type: ignore[assignment]
    now = datetime.now(timezone.utc)

    engine.last_run_at = now - timedelta(seconds=30)
    assert engine.health()["ok"] is True

    engine.last_run_at = now - timedelta(seconds=1000)
    h = engine.health()
    assert h["ok"] is False
    assert h["stale_after_seconds"] == 180
