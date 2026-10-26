"""Wheatley Built-in Check Plugins Package"""

from wheatley.checks.builtin.command import CommandCheck
from wheatley.checks.builtin.docker import DockerCheck
from wheatley.checks.builtin.guest_state import GuestStateCheck
from wheatley.checks.builtin.http import HttpCheck
from wheatley.checks.builtin.lvm_thin import LvmThinCheck
from wheatley.checks.builtin.node_metrics import NodeMetricsCheck
from wheatley.checks.builtin.sensors import SensorsCheck
from wheatley.checks.builtin.smart import SmartCheck
from wheatley.checks.builtin.storage import StorageCheck
from wheatley.checks.builtin.systemd import SystemdCheck
from wheatley.checks.builtin.tcp import TcpCheck
from wheatley.checks.builtin.zfs import ZfsCheck

__all__ = [
    "CommandCheck",
    "DockerCheck",
    "GuestStateCheck",
    "HttpCheck",
    "LvmThinCheck",
    "NodeMetricsCheck",
    "SensorsCheck",
    "SmartCheck",
    "StorageCheck",
    "SystemdCheck",
    "TcpCheck",
    "ZfsCheck",
]
