"""Wheatley Gemini Integration (Maintained for backwards-compatibility; see wheatley.llm)"""

from __future__ import annotations

from wheatley.llm.agent import LLMAgent
from wheatley.llm.providers.gemini import GeminiProvider

# Backwards-compatible alias for GeminiAgent
class GeminiAgent(LLMAgent):
    """Shim class maintaining backwards compatibility with pre-agnostic Wheatley."""
    pass

__all__ = ["GeminiAgent", "LLMAgent", "GeminiProvider"]
