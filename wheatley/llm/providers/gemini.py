"""Wheatley LLM Provider - Google Gemini (Native SDK)"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    types = None  # type: ignore

from wheatley.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, ToolCall, ToolDefinition

logger = logging.getLogger("wheatley.llm.gemini")


class GeminiProvider(BaseLLMProvider):
    """Google Gemini provider using the official google-genai SDK."""

    def __init__(
        self,
        model: str = "gemini-3.8-flash",
        api_key: Optional[str] = None,
        temperature: float = 0.2,
    ):
        self._model = model or "gemini-3.8-flash"
        self._temperature = temperature
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.client: Optional[Any] = None

        if GENAI_AVAILABLE and self._api_key and "xxxx" not in self._api_key:
            try:
                self.client = genai.Client(api_key=self._api_key)
                logger.info("Gemini provider initialized with model '%s'", self._model)
            except Exception as e:
                logger.warning("Could not initialize Google GenAI Client: %s", e)

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def is_configured(self) -> bool:
        return bool(GENAI_AVAILABLE and self.client is not None and self._api_key and "xxxx" not in self._api_key)

    def _convert_tools(self, tools: List[ToolDefinition]) -> List[Any]:
        """Convert ToolDefinitions into Gemini SDK Tool types."""
        if not tools or not GENAI_AVAILABLE:
            return []

        declarations = []
        for td in tools:
            declarations.append(
                types.FunctionDeclaration(
                    name=td.name,
                    description=td.description,
                    parameters=td.parameters,
                )
            )
        return [types.Tool(function_declarations=declarations)]

    def _convert_messages(self, messages: List[LLMMessage]) -> List[Any]:
        """Convert universal LLMMessages into Gemini SDK Content objects."""
        if not GENAI_AVAILABLE:
            return []

        contents: List[Any] = []
        for msg in messages:
            if msg.role == "user":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=msg.content or "")],
                    )
                )
            elif msg.role == "assistant":
                if msg.raw is not None and isinstance(msg.raw, types.Content):
                    contents.append(msg.raw)
                else:
                    parts: List[Any] = []
                    if msg.content:
                        parts.append(types.Part.from_text(text=msg.content))
                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            parts.append(
                                types.Part.from_function_call(
                                    name=tc.name,
                                    args=tc.arguments,
                                )
                            )
                    contents.append(types.Content(role="model", parts=parts))
            elif msg.role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=msg.name or "unknown",
                                response={"result": msg.content},
                            )
                        ],
                    )
                )
        return contents

    async def generate_response(
        self,
        messages: List[LLMMessage],
        tools: List[ToolDefinition],
        system_instruction: str,
    ) -> LLMResponse:
        """Call Gemini API via run_in_executor with transient retry logic."""
        if not self.is_configured:
            raise RuntimeError("Gemini provider is not configured. Provide GEMINI_API_KEY.")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._sync_generate,
            messages,
            tools,
            system_instruction,
        )

    def _sync_generate(
        self,
        messages: List[LLMMessage],
        tools: List[ToolDefinition],
        system_instruction: str,
    ) -> LLMResponse:
        gemini_tools = self._convert_tools(tools)
        gemini_contents = self._convert_messages(messages)

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=gemini_tools if gemini_tools else None,
            temperature=self._temperature,
        )

        try:
            response = self.client.models.generate_content(
                model=self._model,
                contents=gemini_contents,
                config=config,
            )
        except Exception as e:
            logger.exception("Gemini API call failed: %s", e)
            raise

        if not response:
            raise RuntimeError("No response from Gemini API.")

        # Extract tokens
        input_tokens = 0
        output_tokens = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
            output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

        # Extract tool calls
        tool_calls: List[ToolCall] = []
        function_calls = getattr(response, "function_calls", None)
        if function_calls:
            for i, fc in enumerate(function_calls):
                tool_calls.append(
                    ToolCall(
                        id=f"gemini_call_{i}",
                        name=fc.name,
                        arguments=dict(fc.args or {}),
                    )
                )

        text_content = response.text if not tool_calls else (response.text or "")
        raw_candidate = response.candidates[0].content if (response.candidates and response.candidates[0].content) else None
        return LLMResponse(
            content=text_content,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw=raw_candidate,
        )
