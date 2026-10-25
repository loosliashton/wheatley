"""Wheatley LLM Interface - Base Classes, Tool Definitions, and Dataclasses"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """Represents a function call requested by the model."""
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ToolDefinition:
    """Represents a tool/function specification in JSON Schema format."""
    name: str
    description: str
    parameters: Dict[str, Any]


@dataclass
class LLMMessage:
    """Universal message format across LLM providers."""
    role: str  # "system", "user", "assistant", "tool"
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    raw: Optional[Any] = None


@dataclass
class LLMResponse:
    """Standardized response from any LLM provider."""
    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Optional[Any] = None


class BaseLLMProvider(ABC):
    """Abstract interface for all LLM backends (Gemini, OpenAI, Ollama, Groq, etc.)."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name identifier of the provider (e.g. 'gemini', 'ollama', 'openai')."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier (e.g. 'gemini-3.8-flash', 'llama3.2', 'gpt-4o-mini')."""
        ...

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Returns True if the provider has all required credentials and endpoints."""
        ...

    @abstractmethod
    async def generate_response(
        self,
        messages: List[LLMMessage],
        tools: List[ToolDefinition],
        system_instruction: str,
    ) -> LLMResponse:
        """Generate a response given the conversation history, available tools, and system prompt."""
        ...
