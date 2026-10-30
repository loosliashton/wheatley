"""Wheatley Chat Interface Abstraction"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine, Dict, Optional


class ChatInterface(ABC):
    """Abstract chat interface to allow Telegram, Discord, or other backends."""

    @abstractmethod
    async def start(self) -> None:
        """Start listening for incoming chat events."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop listening and clean up resources."""
        pass

    @abstractmethod
    async def send_message(self, text: str, parse_mode: Optional[str] = "Markdown") -> Any:
        """Send a standard message to the primary chat/channel."""
        pass

    @abstractmethod
    async def send_proposal(
        self,
        proposal_id: str,
        summary: str,
        command: str,
        target: str,
        risk: str,
        proposal_type: str = "ACTION",
        pattern: Optional[str] = None,
        justification: Optional[str] = None,
        chat_id: Optional[int] = None,
    ) -> Any:
        """Send an interactive action or allowlist proposal with buttons."""
        pass

    @abstractmethod
    def set_command_handler(
        self, command: str, handler: Callable[..., Coroutine[Any, Any, None]]
    ) -> None:
        """Register a slash-command handler (e.g. /status, /audit)."""
        pass

    def health(self) -> Dict[str, Any]:
        """Return health status of the chat adapter for watchdog checks."""
        return {"ok": True}
