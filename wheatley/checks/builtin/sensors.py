"""Hardware Temperature Sensors Check Plugin

Monitors CPU, NVMe, and motherboard temperatures via lm-sensors (sensors -j).
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Tuple
from wheatley.checks.base import BaseCheck, CheckContext, CheckResult, CheckStatus, register_check

logger = logging.getLogger("wheatley.checks.builtin.sensors")


@register_check("sensors")
class SensorsCheck(BaseCheck):
    """Monitors hardware temperatures against warning and critical thresholds."""

    async def run(self, context: CheckContext) -> CheckResult:
        if not context.shell:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="ShellExecutor is not configured in CheckContext",
            )

        node = self.params.get("node")
        if not node:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary="Missing required 'node' parameter",
            )

        warn_temp = float(self.params.get("warn_temp_c", 80.0))
        crit_temp = float(self.params.get("crit_temp_c", 90.0))

        res = await context.shell.execute_on_host(node, "sensors -j", timeout=10)
        if not res.success or not res.stdout.strip():
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary=f"Failed to read hardware sensors on {node}: {res.stderr.strip() or 'no output'}",
                details={"node": node, "error": res.stderr},
            )

        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.UNKNOWN,
                summary=f"Failed to parse sensors JSON on {node}: {e}",
                details={"node": node, "raw_stdout": res.stdout[:300]},
            )

        # Recursively find all temperature readings
        readings: List[Tuple[str, float]] = []

        def extract_temps(obj: Any, path: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    subpath = f"{path} > {k}" if path else k
                    if isinstance(v, (int, float)) and ("input" in k or "temp" in k):
                        readings.append((subpath, float(v)))
                    else:
                        extract_temps(v, subpath)

        extract_temps(data)

        if not readings:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.OK,
                summary=f"No temperature sensor inputs found on {node}",
                details={"node": node},
            )

        max_label, max_val = max(readings, key=lambda x: x[1])

        issues: List[str] = []
        for label, val in readings:
            if val >= crit_temp:
                issues.append(f"{label}: {val:.1f}°C (CRIT >= {crit_temp}°C)")
            elif val >= warn_temp:
                issues.append(f"{label}: {val:.1f}°C (WARN >= {warn_temp}°C)")

        details = {
            "node": node,
            "max_temp_c": max_val,
            "max_sensor": max_label,
            "readings_count": len(readings),
            "top_sensors": [
                {"sensor": lbl, "temp_c": val}
                for lbl, val in sorted(readings, key=lambda x: x[1], reverse=True)[:5]
            ],
        }

        if any("CRIT" in iss for iss in issues):
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.CRIT,
                summary=f"Critical temperature on {node} ({max_val:.1f}°C): " + "; ".join(issues[:3]),
                details=details,
            )
        elif issues:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.WARN,
                summary=f"High temperature on {node} ({max_val:.1f}°C): " + "; ".join(issues[:3]),
                details=details,
            )

        return CheckResult(
            check_id=self.id,
            status=CheckStatus.OK,
            summary=f"Hardware temperatures normal on {node} (Peak: {max_val:.1f}°C on {max_label.split('>')[-2].strip() if '>' in max_label else max_label})",
            details=details,
        )
