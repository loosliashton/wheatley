"""TCP Port Health Check Plugin"""

from __future__ import annotations

import asyncio
import time
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check


@register_check("tcp")
class TcpCheck(BaseCheck):
    """Probes whether a TCP port is open and accepting connections."""

    async def run(self, context: CheckContext) -> CheckResult:
        raw_host = self.params.get("host", "")
        port = int(self.params.get("port", 0))
        timeout = float(self.params.get("timeout_seconds", 3.0))

        if not raw_host or not port:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'host' or 'port' parameter",
            )

        resolved_host = context.resolve_string(raw_host)
        if "{guest:" in resolved_host:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Failed resolving guest IP for host: {raw_host}",
            )

        start_time = time.perf_counter()
        try:
            conn = asyncio.open_connection(resolved_host, port)
            reader, writer = await asyncio.wait_for(conn, timeout=timeout)
            latency_ms = round((time.perf_counter() - start_time) * 1000, 1)
            writer.close()
            await writer.wait_closed()

            return CheckResult(
                check_id=self.id,
                status=CheckStatus.OK,
                summary=f"Port {port} open ({latency_ms}ms)",
                metrics={"latency_ms": latency_ms},
                details={"host": resolved_host, "port": port},
            )
        except Exception as e:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Port {port} unreachable: {e}",
                details={"host": resolved_host, "port": port},
            )
