"""Wheatley Configuration Schema & Loader"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator
import yaml


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


class BudgetConfig(BaseModel):
    monthly_grant_usd: float = 10.0
    starting_bank_usd: float = 0.0
    warn_threshold_usd: float = 6.0
    flash_only_threshold_usd: float = 8.0
    pause_threshold_usd: float = 9.50
    daily_cap_usd: float = 2.0
    model_flash: str = "gemini-3.8-flash"
    model_pro: str = "gemini-3.8-flash"
    input_cost_per_million: float = 0.15
    output_cost_per_million: float = 0.60
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


class WheatleyConfig(BaseModel):
    nodes: List[NodeConfig] = Field(default_factory=list)
    self_guest: Optional[str] = Field(None, description="e.g. cave/110 to protect agent self")
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    chat: ChatConfig
    checks: List[CheckConfig] = Field(default_factory=list)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
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

    return WheatleyConfig.model_validate(data)
