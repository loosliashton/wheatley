"""Wheatley LLM Provider Factory"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wheatley.config import LLMConfig
    from wheatley.llm.base import BaseLLMProvider

logger = logging.getLogger("wheatley.llm.providers")


def get_llm_provider(config: LLMConfig) -> BaseLLMProvider:
    """Factory creating the appropriate LLM provider based on configuration."""
    provider_type = (config.provider or "gemini").strip().lower()

    if provider_type in ("gemini", "google"):
        from wheatley.llm.providers.gemini import GeminiProvider
        return GeminiProvider(
            model=config.model,
            api_key=config.api_key,
            temperature=config.temperature,
        )

    # Any OpenAI-compatible provider (Ollama, OpenAI, Groq, DeepSeek, LocalAI, vLLM)
    from wheatley.llm.providers.openai_compatible import OpenAICompatibleProvider
    return OpenAICompatibleProvider(
        model=config.model,
        provider_name=provider_type,
        api_key=config.api_key,
        api_base=config.api_base,
        temperature=config.temperature,
    )
