"""Wheatley Configuration Schema & Loader"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
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


class NodeSshConfig(BaseModel):
    host: str
    port: int = 22
    user: str = "wheatley"
    key_path: Optional[str] = None


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

    @model_validator(mode="before")
    @classmethod
    def alias_base_url(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "base_url" in data and "api_base" not in data:
                data["api_base"] = data.pop("base_url")
        return data


class BudgetConfig(BaseModel):
    """Deprecated: Legacy budget config retained for backwards compatibility."""
    monthly_grant_usd: float = 0.0
    starting_bank_usd: float = 0.0
    warn_threshold_usd: float = 0.0
    flash_only_threshold_usd: float = 0.0
    pause_threshold_usd: float = 0.0
    daily_cap_usd: float = 0.0
    model_flash: str = "gemini-3.8-flash"
    model_pro: str = "gemini-3.8-flash"
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    max_tool_iterations: int = 10


class TelegramConfig(BaseModel):
    bot_token: str
    allowed_user_ids: List[int]
    pin: Optional[str] = None

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, v: Any) -> List[int]:
        if isinstance(v, (int, str)):
            v = [v]
        res = []
        for item in (v or []):
            if item is not None and str(item).strip():
                try:
                    res.append(int(item))
                except ValueError:
                    pass
        return res


class ChatConfig(BaseModel):
    telegram: Optional[TelegramConfig] = None


class CheckConfig(BaseModel):
    id: str
    type: str
    enabled: bool = True
    interval_seconds: int = 60
    params: Dict[str, Any] = Field(default_factory=dict)


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
    self_guest: Optional[str] = Field(None, description="e.g. cave/110 to protect agent self")
    llm: LLMConfig = Field(default_factory=LLMConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    chat: ChatConfig
    checks_file: Optional[str] = Field(None, description="Path to external checks.yaml file")
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

    @model_validator(mode="before")
    @classmethod
    def handle_legacy_budget(cls, data: Any) -> Any:
        """Map legacy budget block to llm block if llm is not explicitly provided."""
        if isinstance(data, dict):
            if "budget" in data and isinstance(data["budget"], dict):
                logger.warning(
                    "The 'budget:' section in configuration is deprecated and billing caps are removed. "
                    "Use 'llm:' instead to configure your provider and model."
                )
                b = data["budget"]
                if "llm" not in data:
                    data["llm"] = {
                        "provider": "gemini",
                        "model": b.get("model_flash", "gemini-3.8-flash"),
                        "max_tool_iterations": b.get("max_tool_iterations", 10),
                    }
            elif "llm" in data and isinstance(data["llm"], dict):
                l_cfg = data["llm"]
                if "budget" not in data:
                    data["budget"] = {
                        "model_flash": l_cfg.get("model", "gemini-3.8-flash"),
                        "max_tool_iterations": l_cfg.get("max_tool_iterations", 10),
                    }
        return data

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
            elif isinstance(checks_data, dict) and "checks" in checks_data:
                data["checks"] = checks_data["checks"]
            data["checks_file"] = str(resolved_checks_file)
            logger.info("Loaded checks from %s", resolved_checks_file)
        except Exception as e:
            logger.error("Failed to load external checks file %s: %s", resolved_checks_file, e)
            raise

    return WheatleyConfig.model_validate(data)
