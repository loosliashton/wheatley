"""Wheatley Webhook Receiver - Proxmox VE Notification & Event Target"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any, Callable, Coroutine, Dict, Optional
from aiohttp import web

from wheatley.audit import AuditLogger
from wheatley.config import WebhookConfig

logger = logging.getLogger("wheatley.webhook")


class WebhookServer:
    """Lightweight HTTP server receiving webhooks from Proxmox VE and external monitors."""

    def __init__(
        self,
        config: WebhookConfig,
        audit: AuditLogger,
        alert_sink: Optional[Callable[[str, Optional[Dict[str, Any]]], Coroutine[Any, Any, None]]] = None,
        health_monitor: Optional[Any] = None,
        serve_webhooks: bool = True,
        serve_health: bool = True,
    ):
        self.config = config
        self.audit = audit
        self.alert_sink = alert_sink
        self.health_monitor = health_monitor
        self.serve_webhooks = serve_webhooks
        self.serve_health = serve_health
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        if self.serve_health:
            self.app.router.add_get("/health", self._handle_health)
        if self.serve_webhooks:
            self.app.router.add_post("/webhook/pve", self._handle_pve)
            self.app.router.add_post("/webhook/generic", self._handle_generic)

    def _is_authorized(self, request: web.Request) -> bool:
        if not self.config.secret_token:
            return True

        # Check Bearer token or custom header or query param
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token == self.config.secret_token:
                return True

        header_token = request.headers.get("X-Wheatley-Token", "")
        if header_token == self.config.secret_token:
            return True

        query_token = request.query.get("token", "")
        if query_token == self.config.secret_token:
            return True

        return False

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Unauthenticated health probe for external watchdogs. 200 = healthy, 503 = degraded."""
        if self.health_monitor is None:
            return web.json_response({"status": "ok", "app": "wheatley"})
        report = self.health_monitor.snapshot()
        status_code = 200 if report["status"] == "ok" else 503
        return web.json_response(report, status=status_code)

    async def _handle_pve(self, request: web.Request) -> web.Response:
        """Handle incoming webhook notification from Proxmox VE (PVE 8.1+ notification target)."""
        if not self._is_authorized(request):
            self.audit.log_event(
                "AUTH",
                f"webhook:{request.remote}",
                "unauthorized_webhook_attempt",
                "DENIED",
                details={"path": request.path},
            )
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            content_type = request.content_type
            if "json" in content_type:
                payload = await request.json()
            elif "form" in content_type:
                post_data = await request.post()
                payload = dict(post_data)
            else:
                raw_text = await request.text()
                try:
                    payload = json.loads(raw_text)
                except Exception:
                    payload = {"raw": raw_text}
        except Exception as e:
            logger.warning("Failed to parse webhook body: %s", e)
            return web.json_response({"error": f"Invalid body: {e}"}, status=400)

        # Proxmox notification target fields
        title = payload.get("title", "Proxmox Notification")
        message = payload.get("message", payload.get("text", ""))
        severity = str(payload.get("severity", "info")).lower()
        data = payload.get("data", {})
        hostname = data.get("hostname", payload.get("hostname", "pve"))
        event_type = data.get("type", payload.get("type", "general"))

        is_failure = severity in ("error", "warning") or any(
            w in (title + message).lower() for w in ("failed", "failure", "error", "fencing", "crit")
        )

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        self.audit.log_event(
            "WEBHOOK",
            f"pve:{hostname}",
            f"pve:{event_type}",
            severity.upper(),
            target=hostname,
            details={"title": title, "message": message, "severity": severity, "data": data},
        )

        is_backup = event_type == "vzdump" or "vzdump" in (title + event_type).lower()

        if is_failure and self.alert_sink:
            if is_backup:
                icon = "💾 🚨 *PROXMOX BACKUP FAILED*" if severity == "error" else "💾 ⚠️ *PROXMOX BACKUP WARNING*"
                vmid = data.get("vmid") or data.get("id")
                guest_info = f"\n• *Guest:* `{vmid}`" if vmid else ""
                msg = (
                    f"🔔 [MONITOR] {icon}\n"
                    f"• *Node:* `{hostname}`"
                    f"{guest_info}\n"
                    f"• *Severity:* `{severity.upper()}`\n"
                    f"• *Time:* `{now_str}`\n"
                )
            else:
                icon = "🚨 *PROXMOX ALERT*" if severity == "error" else "⚠️ *PROXMOX WARNING*"
                msg = (
                    f"🔔 [MONITOR] {icon}\n"
                    f"• *Event:* `{title}`\n"
                    f"• *Node:* `{hostname}`\n"
                    f"• *Severity:* `{severity.upper()}`\n"
                    f"• *Time:* `{now_str}`\n"
                )
            if message:
                msg += f"\n*Details:*\n```{message[:500]}```"

            extra = {"type": "webhook_alert", "source": "pve", "event_type": event_type}
            await self.alert_sink(msg, extra)

        return web.json_response({"status": "received", "event": event_type, "severity": severity})

    async def _handle_generic(self, request: web.Request) -> web.Response:
        """Handle generic webhook payload."""
        if not self._is_authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            payload = {"text": await request.text()}

        self.audit.log_event(
            "WEBHOOK",
            f"generic:{request.remote}",
            "generic_webhook",
            "RECEIVED",
            details=payload,
        )

        if self.alert_sink and payload.get("alert"):
            summary = payload.get("summary", "Generic webhook alert triggered")
            msg = f"🔔 [MONITOR] ⚠️ *WEBHOOK ALERT*\n• *Summary:* {summary}\n• *Source:* `{request.remote}`"
            await self.alert_sink(msg, {"type": "webhook_alert", "source": "generic"})

        return web.json_response({"status": "received"})

    async def start(self) -> None:
        """Start the async webhook server."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.config.host, self.config.port)
        await self.site.start()
        logger.info("Webhook server listening on http://%s:%d", self.config.host, self.config.port)

    async def stop(self) -> None:
        """Stop the async webhook server."""
        if self.runner:
            logger.info("Stopping webhook server...")
            await self.runner.cleanup()
            self.runner = None
            self.site = None
