"""Wheatley Check Engine - Base Interfaces & Context"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
import logging
import re
from typing import Any, Callable, Dict, Optional, Type
from pydantic import BaseModel, Field

from wheatley.config import CheckConfig, WheatleyConfig
from wheatley.tools.proxmox import ProxmoxClient
from wheatley.tools.shell import ShellExecutor

logger = logging.getLogger("wheatley.checks.base")


class CheckStatus(str, Enum):
    OK = "OK"
    WARN = "WARN"
    CRIT = "CRIT"
    UNKNOWN = "UNKNOWN"


class CheckResult(BaseModel):
    check_id: str
    status: CheckStatus
    summary: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    details: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_healthy(self) -> bool:
        return self.status == CheckStatus.OK


class CheckContext:
    """Runtime context providing access to Proxmox clients, shell executor, and IP resolution."""

    def __init__(
        self,
        config: WheatleyConfig,
        proxmox_clients: Dict[str, ProxmoxClient],
        shell: Optional[ShellExecutor] = None,
    ):
        self.config = config
        self.clients = proxmox_clients
        self.shell = shell
        self._ip_cache: Dict[str, str] = {}

    def get_client(self, node_name: str) -> Optional[ProxmoxClient]:
        return self.clients.get(node_name)

    def resolve_guest_ip(self, guest_spec: str) -> Optional[str]:
        """Resolves guest specification e.g. 'pve-node-01/102' to a live IPv4 address."""
        spec = guest_spec.strip()
        if spec.startswith("{guest:") and spec.endswith("}"):
            spec = spec[7:-1].strip()

        if spec in self._ip_cache:
            return self._ip_cache[spec]

        if "/" not in spec:
            return None
        node_name, vmid_str = spec.split("/", 1)
        client = self.get_client(node_name)
        if not client:
            return None

        try:
            vmid = int(vmid_str)
            # Find whether guest is qemu or lxc
            guests = client.get_guests()
            guest_type = "qemu"
            for g in guests:
                if g.get("vmid") == vmid:
                    guest_type = g.get("type", "qemu")
                    break

            ips = client.get_guest_ips(vmid, guest_type)
            if ips:
                resolved = ips[0]
                self._ip_cache[spec] = resolved
                return resolved
        except Exception as e:
            logger.warning("Failed to resolve IP for guest %s: %s", spec, e)

        return None

    def resolve_string(self, text: str) -> str:
        """Replaces all occurrences of {guest:node/vmid} with resolved IP addresses."""
        pattern = re.compile(r"\{guest:([a-zA-Z0-9_\-]+/[0-9]+)\}")

        def replace_match(m: re.Match) -> str:
            spec = m.group(1)
            ip = self.resolve_guest_ip(spec)
            if ip:
                return ip
            logger.warning("Could not resolve IP for %s in string '%s'", spec, text)
            return m.group(0)

        return pattern.sub(replace_match, text)


class BaseCheck(ABC):
    """Abstract base class for all Wheatley check types."""

    def __init__(self, config: Optional[CheckConfig] = None, **kwargs: Any):
        if config is not None:
            self.config = config
            self.id = config.id
            self.type = config.type
            self.params = config.params or {}
        else:
            self.id = kwargs.get("id", "check")
            self.type = kwargs.get("type", "generic")
            self.params = kwargs.get("params", {})
            self.config = CheckConfig(id=self.id, type=self.type, params=self.params)

    @abstractmethod
    async def run(self, context: CheckContext) -> CheckResult:
        """Execute the check and return CheckResult."""
        pass


# Global Check Registry
CHECK_REGISTRY: Dict[str, Type[BaseCheck]] = {}
_PLUGINS_LOADED: bool = False


def load_plugin_checks(force_reload: bool = False) -> None:
    """Discover and register external checks from installed packages via entry points."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED and not force_reload:
        return
    _PLUGINS_LOADED = True

    try:
        from importlib.metadata import entry_points
        eps = entry_points(group="wheatley.checks")
        for ep in eps:
            try:
                check_cls = ep.load()
                if isinstance(check_cls, type) and issubclass(check_cls, BaseCheck):
                    CHECK_REGISTRY[ep.name] = check_cls
                    logger.info("Loaded plugin check '%s' from %s", ep.name, ep.value)
                else:
                    logger.warning("Plugin '%s' is not a subclass of BaseCheck", ep.name)
            except Exception as e:
                logger.warning("Failed to load plugin check '%s': %s", ep.name, e)
    except Exception as e:
        logger.warning("Error loading plugin entry points: %s", e)


def register_check(type_name: str) -> Callable[[Type[BaseCheck]], Type[BaseCheck]]:
    """Decorator to register a check type."""
    def decorator(cls: Type[BaseCheck]) -> Type[BaseCheck]:
        CHECK_REGISTRY[type_name] = cls
        return cls
    return decorator


def get_check_class(type_name: str) -> Optional[Type[BaseCheck]]:
    """Get check implementation class by type name."""
    if type_name not in CHECK_REGISTRY:
        load_plugin_checks()
    return CHECK_REGISTRY.get(type_name)
