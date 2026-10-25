"""Self-health reporting for external watchdogs.

Wheatley can't tell you it's down, so an external watchdog has to notice
silence. This module supports both common watchdog styles:

* **Pull** - the watchdog polls ``GET /health`` (served by ``WebhookServer``).
  Returns HTTP 200 when healthy and 503 when degraded.
* **Push** - Wheatley requests ``health.push_url`` on an interval, *only while
  healthy*. The watchdog alerts when pings stop arriving.

The response body deliberately contains no hostnames, IPs, or check names, so the
endpoint is safe to expose without authentication.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Callable, Dict, Optional

import httpx

from wheatley import __version__
from wheatley.config import HealthConfig

logger = logging.getLogger("wheatley.health")

# A component probe returns a dict that must include a boolean "ok" key.
ComponentProbe = Callable[[], Dict[str, Any]]


class HealthMonitor:
    """Aggregates component probes into one health report, and optionally pushes heartbeats."""

    def __init__(self, config: HealthConfig):
        self.config = config
        self.started_at = datetime.now(timezone.utc)
        self._probes: Dict[str, ComponentProbe] = {}
        self._push_task: Optional[asyncio.Task] = None
        self.last_push_ok: Optional[bool] = None

    def register(self, name: str, probe: ComponentProbe) -> None:
        """Register a component probe, e.g. ``monitor.register("check_engine", engine.health)``."""
        self._probes[name] = probe

    def snapshot(self) -> Dict[str, Any]:
        """Run all probes. A probe that raises counts as unhealthy."""
        components: Dict[str, Any] = {}
        for name, probe in self._probes.items():
            try:
                result = dict(probe())
                result["ok"] = bool(result.get("ok", False))
            except Exception as e:  # never let a probe crash the health endpoint
                logger.warning("Health probe '%s' failed: %s", name, e)
                result = {"ok": False, "error": type(e).__name__}
            components[name] = result

        healthy = all(c["ok"] for c in components.values())
        uptime = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        return {
            "status": "ok" if healthy else "degraded",
            "app": "wheatley",
            "version": __version__,
            "uptime_seconds": int(uptime),
            "components": components,
        }

    @property
    def push_enabled(self) -> bool:
        return bool(self.config.push_url and self.config.push_url.strip())

    async def push_once(self, client: Optional[httpx.AsyncClient] = None) -> bool:
        """Send one heartbeat if healthy. Returns True if a ping was sent successfully."""
        if not self.push_enabled:
            return False

        report = self.snapshot()
        if report["status"] != "ok":
            degraded = [n for n, c in report["components"].items() if not c["ok"]]
            logger.warning("Skipping heartbeat push: degraded components %s", degraded)
            self.last_push_ok = False
            return False

        url = self.config.push_url.strip()  # type: ignore[union-attr]
        method = self.config.push_method.upper()
        own_client = client is None
        client = client or httpx.AsyncClient(timeout=self.config.push_timeout_seconds)
        try:
            if method == "POST":
                resp = await client.post(url, json=report)
            else:
                resp = await client.get(url)
            ok = resp.status_code < 400
            if not ok:
                # The URL usually embeds a secret token, so never log it.
                logger.warning("Heartbeat push rejected with HTTP %s", resp.status_code)
            self.last_push_ok = ok
            return ok
        except Exception as e:
            logger.warning("Heartbeat push failed: %s", type(e).__name__)
            self.last_push_ok = False
            return False
        finally:
            if own_client:
                await client.aclose()

    async def _push_loop(self) -> None:
        interval = self.config.push_interval_seconds
        async with httpx.AsyncClient(timeout=self.config.push_timeout_seconds) as client:
            while True:
                await self.push_once(client)
                await asyncio.sleep(interval)

    def start(self) -> None:
        if self.push_enabled and self._push_task is None:
            logger.info("Heartbeat push enabled (every %ds).", self.config.push_interval_seconds)
            self._push_task = asyncio.create_task(self._push_loop(), name="wheatley-heartbeat")

    async def stop(self) -> None:
        if self._push_task:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
            self._push_task = None
