"""Wheatley LLM Provider - OpenAI-Compatible Endpoints (Ollama, OpenAI, Groq, DeepSeek, LocalAI, vLLM)"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional
import httpx

from wheatley.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, ToolCall, ToolDefinition

logger = logging.getLogger("wheatley.llm.openai_compatible")


class OpenAICompatibleProvider(BaseLLMProvider):
    """Universal provider for any model offering an OpenAI-compatible /v1/chat/completions REST endpoint."""

    def __init__(
        self,
        model: str = "llama3.2",
        provider_name: str = "openai_compatible",
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ):
        self._provider_name = provider_name.lower()
        self._model = model or "llama3.2"
        self._temperature = temperature
        self._timeout = timeout

        # Resolve api_base with sensible defaults
        if api_base:
            self._api_base = api_base.rstrip("/")
        elif self._provider_name == "ollama":
            self._api_base = "http://localhost:11434/v1"
        elif self._provider_name == "groq":
            self._api_base = "https://api.groq.com/openai/v1"
        elif self._provider_name == "openai":
            self._api_base = "https://api.openai.com/v1"
        else:
            self._api_base = "http://localhost:11434/v1"

        # Resolve api_key
        if api_key:
            self._api_key = api_key
        elif self._provider_name == "groq":
            self._api_key = os.environ.get("GROQ_API_KEY", "")
        elif self._provider_name == "openai":
            self._api_key = os.environ.get("OPENAI_API_KEY", "")
        elif self._provider_name == "ollama":
            self._api_key = os.environ.get("OPENAI_API_KEY", "ollama")
        else:
            self._api_key = os.environ.get("OPENAI_API_KEY", "none")

        logger.info(
            "OpenAI-compatible provider '%s' initialized (endpoint: %s, model: %s)",
            self._provider_name,
            self._api_base,
            self._model,
        )

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def is_configured(self) -> bool:
        # For Ollama / local, no secret key is required; for cloud providers, key must be non-empty and non-placeholder
        if self._provider_name in ("ollama", "localai", "vllm"):
            return bool(self._api_base)
        return bool(self._api_key and "xxxx" not in self._api_key and self._api_base)

    def _convert_tools(self, tools: List[ToolDefinition]) -> List[Dict[str, Any]]:
        """Format tools into standard OpenAI function specification."""
        if not tools:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def _convert_messages(
        self,
        messages: List[LLMMessage],
        system_instruction: str,
    ) -> List[Dict[str, Any]]:
        """Format universal messages into OpenAI chat completions payload."""
        payload: List[Dict[str, Any]] = []

        if system_instruction:
            payload.append({"role": "system", "content": system_instruction})

        for msg in messages:
            if msg.role == "user":
                payload.append({"role": "user", "content": msg.content or ""})
            elif msg.role == "assistant":
                entry: Dict[str, Any] = {"role": "assistant"}
                if msg.content:
                    entry["content"] = msg.content
                else:
                    entry["content"] = None
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                payload.append(entry)
            elif msg.role == "tool":
                content_str = (
                    json.dumps(msg.content, default=str)
                    if not isinstance(msg.content, str)
                    else msg.content
                )
                payload.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id or "call_0",
                        "name": msg.name or "unknown",
                        "content": content_str,
                    }
                )

        return payload

    async def generate_response(
        self,
        messages: List[LLMMessage],
        tools: List[ToolDefinition],
        system_instruction: str,
    ) -> LLMResponse:
        """Issue asynchronous POST request to /v1/chat/completions."""
        endpoint = f"{self._api_base}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        openai_tools = self._convert_tools(tools)
        openai_messages = self._convert_messages(messages, system_instruction)

        body: Dict[str, Any] = {
            "model": self._model,
            "messages": openai_messages,
            "temperature": self._temperature,
        }
        if openai_tools:
            body["tools"] = openai_tools

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(endpoint, headers=headers, json=body)
            except httpx.ConnectError as e:
                raise RuntimeError(
                    f"Could not connect to {self._provider_name} at {endpoint}. "
                    f"Ensure service is running and accessible: {e}"
                ) from e
            except httpx.TimeoutException as e:
                raise RuntimeError(f"Request to {self._provider_name} at {endpoint} timed out: {e}") from e

            if resp.status_code != 200:
                raise RuntimeError(
                    f"LLM API Error ({self._provider_name} HTTP {resp.status_code}): {resp.text}"
                )

            data = resp.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"No choices returned by {self._provider_name}: {data}")

        first_choice = choices[0].get("message", {})
        content = first_choice.get("content")
        raw_tool_calls = first_choice.get("tool_calls") or []

        tool_calls: List[ToolCall] = []
        for i, tc in enumerate(raw_tool_calls):
            tc_id = tc.get("id") or f"call_{i}"
            fn = tc.get("function") or {}
            name = fn.get("name", "unknown")
            args_raw = fn.get("arguments", "{}")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {}
            else:
                args = dict(args_raw or {})
            tool_calls.append(ToolCall(id=tc_id, name=name, arguments=args))

        usage = data.get("usage") or {}
        input_tokens = usage.get("prompt_tokens", 0) or 0
        output_tokens = usage.get("completion_tokens", 0) or 0

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
