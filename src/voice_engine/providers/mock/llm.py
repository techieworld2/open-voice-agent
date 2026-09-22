"""Deterministic mock LLM."""

import asyncio
from typing import AsyncIterator
from voice_engine.core.types import ChatMessage, LLMToken
from voice_engine.interfaces.llm import BaseLLM
from voice_engine.providers.registry import register


@register("llm", "mock")
class MockLLM(BaseLLM):
    """Stream a deterministic response."""

    def __init__(self, response: str = "Hello! This is a local streaming voice engine.") -> None:
        self.response = response

    async def generate(self, messages: list[ChatMessage]) -> AsyncIterator[LLMToken]:
        for word in self.response.split():
            await asyncio.sleep(0.01)
            yield LLMToken(word + " ")
