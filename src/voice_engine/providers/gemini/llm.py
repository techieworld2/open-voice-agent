"""Gemini LLM adapter (Google GenAI)."""

import os
from typing import AsyncIterator

from voice_engine.core.types import ChatMessage, LLMToken
from voice_engine.interfaces.llm import BaseLLM
from voice_engine.providers.registry import register


@register("llm", "gemini")
class GeminiLLM(BaseLLM):
    """Stream tokens from the Gemini API's async chat-completion client."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> None:
        self.model = model or os.getenv("VOICE_ENGINE_GEMINI_LLM_MODEL", "gemini-3.6-flash")
        self._api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else os.getenv(
                "VOICE_ENGINE_LLM_SYSTEM_PROMPT",
                "You are a helpful, concise voice assistant. Keep replies short.",
            )
        )
        self.max_output_tokens = max_output_tokens or int(
            os.getenv("VOICE_ENGINE_GEMINI_LLM_MAX_TOKENS", "200")
        )
        self.temperature = (
            temperature
            if temperature is not None
            else float(os.getenv("VOICE_ENGINE_GEMINI_LLM_TEMPERATURE", "0.7"))
        )
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError(
                    "Install optional dependencies with: pip install -e '.[gemini]'"
                ) from exc
            if not self._api_key:
                raise RuntimeError("Set GEMINI_API_KEY to use the gemini LLM provider.")
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def generate(self, messages: list[ChatMessage]) -> AsyncIterator[LLMToken]:
        from google.genai import types

        client = self._get_client()
        contents = [
            types.Content(
                role="model" if message.role == "assistant" else "user",
                parts=[types.Part(text=message.content)],
            )
            for message in messages
        ]
        config = types.GenerateContentConfig(
            system_instruction=self.system_prompt,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
        )
        stream = await client.aio.models.generate_content_stream(
            model=self.model, contents=contents, config=config
        )
        async for chunk in stream:
            if chunk.text:
                yield LLMToken(chunk.text)

    async def close(self) -> None:
        pass
