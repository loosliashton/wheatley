"""HTTP/HTTPS Service Health Check Plugin"""

from __future__ import annotations

import time
import httpx

from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check


@register_check("http")
class HttpCheck(BaseCheck):
    """Probes an HTTP or HTTPS endpoint with dynamic guest IP resolution."""

    async def run(self, context: CheckContext) -> CheckResult:
        raw_url = self.params.get("url", "")
        if not raw_url:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'url' parameter",
            )

        resolved_url = context.resolve_string(raw_url)
        if "{guest:" in resolved_url:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Failed to resolve guest IP in URL: {raw_url}",
                details={"unresolved_url": resolved_url},
            )

        expected_status = self.params.get("expected_status", [200])
        if isinstance(expected_status, int):
            expected_status = [expected_status]

        timeout = float(self.params.get("timeout_seconds", 5.0))
        tls_verify = bool(self.params.get("tls_verify", True))
        match_body = self.params.get("match_body")
        method = self.params.get("method", "GET").upper()

        start_time = time.perf_counter()
        try:
            async with httpx.AsyncClient(verify=tls_verify, timeout=timeout, follow_redirects=True) as client:
                res = await client.request(method, resolved_url)
                latency_ms = round((time.perf_counter() - start_time) * 1000, 1)

                if res.status_code not in expected_status:
                    return CheckResult(
                        check_id=self.id,
                        status=CheckStatus.CRIT,
                        summary=f"HTTP {res.status_code} (expected {expected_status}) in {latency_ms}ms",
                        metrics={"latency_ms": latency_ms, "status_code": res.status_code},
                        details={"url": resolved_url, "response_snippet": res.text[:200]},
                    )

                if match_body and match_body not in res.text:
                    return CheckResult(
                        check_id=self.id,
                        status=CheckStatus.CRIT,
                        summary=f"Body signature '{match_body}' not found in {latency_ms}ms response",
                        metrics={"latency_ms": latency_ms, "status_code": res.status_code},
                        details={"url": resolved_url},
                    )

                return CheckResult(
                    check_id=self.id,
                    status=CheckStatus.OK,
                    summary=f"HTTP {res.status_code} OK ({latency_ms}ms)",
                    metrics={"latency_ms": latency_ms, "status_code": res.status_code},
                    details={"url": resolved_url},
                )

        except httpx.ConnectTimeout:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Connection timed out after {timeout}s",
                details={"url": resolved_url},
            )
        except httpx.ConnectError as e:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Connection refused / failed: {e}",
                details={"url": resolved_url},
            )
        except Exception as e:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"HTTP check error: {e}",
                details={"url": resolved_url},
            )
