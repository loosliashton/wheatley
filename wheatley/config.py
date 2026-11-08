"""Wheatley Configuration Schema & Loader"""

from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field, field_validator, model_validator
import yaml

logger = logging.getLogger("wheatley.config")


ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-(.*?))?\}")


def interpolate_env_vars(raw: str) -> str:
    """Replace ${VAR} or ${VAR:-default} with environment variables."""
    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_val = match.group(2) if match.group(2) is not None else ""
        return os.environ.get(var_name, default_val)
    return ENV_VAR_PATTERN.sub(replacer, raw)


def normalize_model_name(name: Any) -> str:
    """Strip whitespace and an optional leading 'models/' prefix from an LLM model name."""
    s = str(name).strip() if name is not None else ""
    if s.startswith("models/"):
        s = s[len("models/"):]
    return s


def normalize_model_list(names: Any, exclude: Optional[str] = None) -> List[str]:
    """Normalize a list of model names, dropping empties, duplicates and the optional excluded model."""
    res: List[str] = []
    for m in names or []:
        s = normalize_model_name(m)
        if s and s != exclude and s not in res:
            res.append(s)
    return res


class NodeSshConfig(BaseModel):
    host: str
    port: int = 22
    user: str = "wheatley"
    key_path: Optional[str] = None
    known_hosts_path: Optional[str] = None


class NodeTokensConfig(BaseModel):
    monitor: str = Field(..., description="Token ID=Secret for PVEAuditor role")
    ops: Optional[str] = Field(None, description="Token ID=Secret for approved ops")


class NodeConfig(BaseModel):
    name: str
    api_url: str
    ssh: NodeSshConfig
    tls_fingerprint: Optional[str] = None
    tokens: NodeTokensConfig


class LLMConfig(BaseModel):
    provider: str = Field(
        default="gemini",
        description="LLM provider: 'gemini', 'openai_compatible', 'ollama', 'openai', 'groq', etc.",
    )
    model: str = Field(
        default="gemini-3.8-flash",
        description="Model name (e.g. 'gemini-3.8-flash', 'llama3.2', 'gpt-4o-mini')",
    )
    fallback_models: List[str] = Field(
        default_factory=list,
        description="Optional fallback models attempted if the primary model returns 503 or 429",
    )
    api_key: Optional[str] = Field(
        None,
        description="API Key (can also be supplied via GEMINI_API_KEY, OPENAI_API_KEY, or GROQ_API_KEY env vars)",
    )
    api_base: Optional[str] = Field(
        None,
        description="API Base URL (e.g. 'http://localhost:11434/v1' for Ollama, 'https://api.openai.com/v1')",
    )
    temperature: float = Field(0.2, description="Inference temperature")
    max_tool_iterations: int = Field(10, description="Max function-calling reasoning turns")
    history_turns: int = Field(10, description="Recent conversation turns retained in memory for context")
    history_retention_days: int = Field(30, description="Days to retain conversation history in SQLite")
    session_timeout_minutes: int = Field(
        60,
        description="Inactivity timeout in minutes after which conversation context auto-resets (0 to disable)",
    )
    max_tool_result_chars: int = Field(4000, description="Max characters for a single tool result before truncation")

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model(cls, v: Any) -> str:
        return normalize_model_name(v) or "gemini-3.8-flash"

    @field_validator("fallback_models", mode="before")
    @classmethod
    def normalize_fallback_models(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            v = v.split(",")
        return normalize_model_list(v or [])

    @model_validator(mode="before")
    @classmethod
    def alias_base_url(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "base_url" in data and "api_base" not in data:
                data["api_base"] = data.pop("base_url")
        return data


class TelegramConfig(BaseModel):
    bot_token: str
    allowed_user_ids: List[int]

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, v: Any) -> List[int]:
        if isinstance(v, (int, str)):
            v = [v]
        res: List[int] = []
        for item in (v or []):
            if item is None:
                continue
            item_str = str(item).strip()
            if not item_str:
                continue
            try:
                res.append(int(item_str))
            except (ValueError, TypeError):
                raise ValueError(
                    f"Invalid Telegram user ID '{item}'. Telegram user IDs must be numeric integers "
                    "(obtain yours from @userinfobot)."
                )
        if not res:
            raise ValueError(
                "Telegram 'allowed_user_ids' cannot be empty. At least one numeric Telegram user ID "
                "is required to authenticate commands (obtain yours from @userinfobot)."
            )
        return res


class ChatConfig(BaseModel):
    telegram: Optional[TelegramConfig] = None


VALID_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_ALIASES = {
    "monday": "mon", "mon": "mon",
    "tuesday": "tue", "tue": "tue", "tues": "tue",
    "wednesday": "wed", "wed": "wed",
    "thursday": "thu", "thu": "thu", "thur": "thu", "thurs": "thu",
    "friday": "fri", "fri": "fri",
    "saturday": "sat", "sat": "sat",
    "sunday": "sun", "sun": "sun",
}
TIME_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_hhmm(value: Any) -> Tuple[int, int]:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1440:
        # Handle YAML 1.1 sexagesimal integer conversion (e.g., unquoted 23:00 -> 1380)
        return divmod(value, 60)
    m = TIME_HHMM_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"Invalid time format '{value}'; expected HH:MM (24-hour, e.g. '02:00')")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time '{value}' out of range (00:00 to 23:59)")
    return hour, minute


class ScheduleWindow(BaseModel):
    """Recurring time window during which monitoring is paused."""

    start: str = Field(..., description="Start time in 24-hour HH:MM format (local time), e.g. '02:00'")
    end: str = Field(..., description="End time in 24-hour HH:MM format (local time), e.g. '04:00'")
    days: Optional[List[str]] = Field(
        None,
        description="Optional days of week (mon, tue, wed, thu, fri, sat, sun). Omit for every day.",
    )
    reason: Optional[str] = Field(None, description="Optional note explaining the scheduled downtime")

    @field_validator("start", "end", mode="before")
    @classmethod
    def normalize_time(cls, v: Any) -> str:
        hour, minute = _parse_hhmm(v)
        return f"{hour:02d}:{minute:02d}"

    @field_validator("days", mode="before")
    @classmethod
    def normalize_days(cls, v: Any) -> Optional[List[str]]:
        if v is None:
            return None
        if isinstance(v, str):
            raw = v.strip().lower()
            if not raw or raw in ("*", "all", "daily", "everyday", "every day"):
                return None
            if raw in ("weekday", "weekdays"):
                return ["mon", "tue", "wed", "thu", "fri"]
            if raw in ("weekend", "weekends"):
                return ["sat", "sun"]
            items = [p.strip() for p in raw.split(",") if p.strip()]
        elif isinstance(v, list):
            items = [str(x).strip().lower() for x in v if str(x).strip()]
        else:
            raise ValueError(f"Invalid days specification: {v}")

        if not items or items == ["*"] or items == ["all"] or items == ["daily"]:
            return None

        normalized: List[str] = []
        for item in items:
            if item in ("weekday", "weekdays"):
                for d in ("mon", "tue", "wed", "thu", "fri"):
                    if d not in normalized:
                        normalized.append(d)
                continue
            if item in ("weekend", "weekends"):
                for d in ("sat", "sun"):
                    if d not in normalized:
                        normalized.append(d)
                continue
            mapped = DAY_ALIASES.get(item)
            if not mapped:
                raise ValueError(f"Invalid day of week '{item}'; expected mon..sun")
            if mapped not in normalized:
                normalized.append(mapped)
        return normalized or None

    def is_active(self, now: Optional[datetime] = None) -> bool:
        """Return True if the given datetime (defaults to current local system time) falls inside this window."""
        dt = now if now is not None else datetime.now().astimezone()
        sh, sm = _parse_hhmm(self.start)
        eh, em = _parse_hhmm(self.end)
        start_mins = sh * 60 + sm
        end_mins = eh * 60 + em
        curr_mins = dt.hour * 60 + dt.minute

        today_day = VALID_DAYS[dt.weekday()]
        prev_day = VALID_DAYS[(dt.weekday() - 1) % 7]

        if start_mins == end_mins:
            # 24-hour window on matching days
            return self.days is None or today_day in self.days

        if start_mins < end_mins:
            # Same-day window (e.g. 02:00 -> 04:00)
            if self.days is not None and today_day not in self.days:
                return False
            return start_mins <= curr_mins < end_mins

        # Overnight window crossing midnight (e.g. 23:00 -> 05:00)
        if curr_mins >= start_mins:
            return self.days is None or today_day in self.days
        if curr_mins < end_mins:
            return self.days is None or prev_day in self.days
        return False

    def format_short(self) -> str:
        """Return a concise human-readable description of this window."""
        days_str = ",".join(self.days) if self.days else "daily"
        base = f"{self.start}-{self.end} ({days_str})"
        if self.reason:
            return f"{base} — {self.reason}"
        return base


class CheckConfig(BaseModel):
    id: str
    type: str
    enabled: bool = True
    interval_seconds: int = 60
    schedule: List[ScheduleWindow] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def map_schedule_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict) and "schedule" not in data:
            for alias in ("pause_windows", "maintenance_windows", "quiet_hours"):
                if alias in data:
                    data = dict(data)
                    data["schedule"] = data.pop(alias)
                    break
        return data


class WebhookConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8088
    secret_token: Optional[str] = None


class HealthConfig(BaseModel):
    """Self-monitoring for external watchdogs (Uptime Kuma, healthchecks.io, Gatus, etc.).

    Two modes, usable together:
      * Pull: GET /health on the HTTP listener (webhook host/port). 200 = healthy, 503 = degraded.
      * Push: if push_url is set, Wheatley requests it every push_interval_seconds while healthy.
    """
    enabled: bool = Field(True, description="Serve GET /health on the HTTP listener")
    stale_after_seconds: Optional[int] = Field(
        None, description="Report degraded if no check has run for this long (default: 3x shortest interval, min 180s)"
    )
    push_url: Optional[str] = Field(None, description="Heartbeat URL to request while healthy (treated as a secret)")
    push_interval_seconds: int = Field(60, ge=10)
    push_method: str = Field("GET", description="GET or POST")
    push_timeout_seconds: float = Field(10.0, gt=0)


class WheatleyConfig(BaseModel):
    nodes: List[NodeConfig] = Field(default_factory=list)
    self_guest: Optional[str] = Field(None, description="e.g. pve-node-01/110 to protect agent self")
    llm: LLMConfig = Field(default_factory=LLMConfig)
    chat: ChatConfig
    checks_file: Optional[str] = Field(None, description="Path to external checks.yaml file")
    schedule: List[ScheduleWindow] = Field(
        default_factory=list,
        description="Global monitoring pause windows loaded from checks.yaml",
    )
    checks: List[CheckConfig] = Field(default_factory=list)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    allowlist: List[str] = Field(
        default_factory=lambda: [
            "uptime",
            "free -m",
            "df -h",
            "top -b -n 1",
            "journalctl -n *",
            "systemctl status *",
            "docker ps",
            "docker logs *",
            "zpool status*",
            "smartctl -H *",
            "sensors -j",
        ]
    )

    @field_validator("nodes")
    @classmethod
    def unique_node_names(cls, nodes: List[NodeConfig]) -> List[NodeConfig]:
        names = [n.name for n in nodes]
        if len(names) != len(set(names)):
            raise ValueError("Node names must be unique")
        return nodes


def load_config(config_path: str | Path, secrets_path: Optional[str | Path] = None) -> WheatleyConfig:
    """Load, interpolate, and validate Wheatley configuration."""
    cfg_file = Path(config_path)
    if not cfg_file.is_file():
        raise FileNotFoundError(f"Config file not found: {cfg_file}")

    # Load secrets env file into os.environ if provided
    if secrets_path:
        sec_file = Path(secrets_path)
        if sec_file.is_file():
            with open(sec_file, "r", encoding="utf-8") as sf:
                for line in sf:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip().strip("'\"")

    with open(cfg_file, "r", encoding="utf-8") as f:
        raw_text = f.read()

    interpolated = interpolate_env_vars(raw_text)
    data = yaml.safe_load(interpolated)
    if not isinstance(data, dict):
        raise ValueError("Config YAML must be a mapping")

    # Enforce separate checks file: checks must NOT be defined directly in config.yaml
    if "checks" in data and data["checks"]:
        raise ValueError(
            "Defining 'checks' directly inside config.yaml is not allowed. "
            "Please place your checks in a separate checks configuration file "
            "(e.g. /etc/wheatley/checks.yaml) and configure 'checks_file' "
            "or place 'checks.yaml' alongside your config file."
        )

    # Load separate checks.yaml if specified (or via CHECKS_FILE env var), or alongside config_path
    checks_file_spec = (
        data.get("checks_file")
        or os.environ.get("CHECKS_FILE")
        or os.environ.get("WHEATLEY_CHECKS_FILE")
    )
    resolved_checks_file: Optional[Path] = None
    if checks_file_spec:
        p = Path(checks_file_spec)
        if not p.is_absolute():
            p = cfg_file.parent / p
        if not p.is_file():
            raise FileNotFoundError(f"Configured checks_file not found: {p}")
        resolved_checks_file = p
    elif (cfg_file.parent / "checks.yaml").is_file():
        resolved_checks_file = cfg_file.parent / "checks.yaml"

    if resolved_checks_file and resolved_checks_file.is_file():
        try:
            with open(resolved_checks_file, "r", encoding="utf-8") as cf:
                raw_checks = cf.read()
            interp_checks = interpolate_env_vars(raw_checks)
            checks_data = yaml.safe_load(interp_checks)
            if isinstance(checks_data, list):
                data["checks"] = checks_data
            elif isinstance(checks_data, dict):
                if "checks" in checks_data:
                    data["checks"] = checks_data["checks"]
                for sched_key in ("schedule", "pause_windows", "maintenance_windows", "quiet_hours"):
                    if sched_key in checks_data and checks_data[sched_key] is not None:
                        data["schedule"] = checks_data[sched_key]
                        break
            data["checks_file"] = str(resolved_checks_file)
            logger.info("Loaded checks from %s", resolved_checks_file)
        except Exception as e:
            logger.error("Failed to load external checks file %s: %s", resolved_checks_file, e)
            raise

    return WheatleyConfig.model_validate(data)
