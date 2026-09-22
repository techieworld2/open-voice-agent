"""Generic OpenAI-compatible /v1/completions streaming adapter.

Works against any server that speaks the legacy OpenAI text-completion API:
vLLM, text-generation-webui, LM Studio, llama.cpp's server, etc.
"""

import json
import os
from typing import AsyncIterator

from voice_engine.core.types import ChatMessage, LLMToken
from voice_engine.interfaces.llm import BaseLLM
from voice_engine.providers.registry import register


def _render_prompt(messages: list[ChatMessage], system_prompt: str) -> str:
    lines = [f"System: {system_prompt}"] if system_prompt else []
    for message in messages:
        role = "User" if message.role == "user" else "Assistant"
        lines.append(f"{role}: {message.content}")
    lines.append("Assistant:")
    return "\n".join(lines)


@register("llm", "openai_completions")
class OpenAICompletionsLLM(BaseLLM):
    """Stream tokens from an OpenAI-compatible /v1/completions endpoint."""

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> None:
        self.url = url or os.getenv(
            "VOICE_ENGINE_LLM_URL", "http://localhost:5000/v1/completions"
        )
        self.model = model or os.getenv("VOICE_ENGINE_LLM_MODEL", "")
        self._api_key = api_key or os.getenv("VOICE_ENGINE_LLM_API_KEY", "")
        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else os.getenv(
                "VOICE_ENGINE_LLM_SYSTEM_PROMPT",
                "You are a helpful, concise voice assistant. Keep replies short.",
            )
        )
        self.max_tokens = max_tokens or int(os.getenv("VOICE_ENGINE_LLM_MAX_TOKENS", "200"))
        self.temperature = (
            temperature if temperature is not None
            else float(os.getenv("VOICE_ENGINE_LLM_TEMPERATURE", "0.7"))
        )
        self._client = None

    def _get_client(self):
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError(
                "Install optional dependencies with: pip install -e '.[openai_completions]'"
            ) from exc
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def generate(self, messages: list[ChatMessage]) -> AsyncIterator[LLMToken]:
        client = self._get_client()
        payload = {
            "prompt": _render_prompt(messages, self.system_prompt),
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
            "stop": ["\nUser:", "\nSystem:"],
        }
        if self.model:
            payload["model"] = self.model
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        async with client.stream("POST", self.url, json=payload, headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data.strip() == "[DONE]":
                    return
                chunk = json.loads(data)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                text = choices[0].get("text", "")
                if text:
                    yield LLMToken(text)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
