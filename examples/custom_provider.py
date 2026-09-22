"""Provider extension example."""

from typing import AsyncIterator
from voice_engine import BaseLLM, BaseSTT, BaseTTS
from voice_engine.core.types import AudioChunk, ChatMessage, LLMToken, TranscriptEvent
from voice_engine.providers.registry import register


@register("llm", "my-llm")
class MyLLM(BaseLLM):
    async def generate(self, messages: list[ChatMessage]) -> AsyncIterator[LLMToken]:
        yield LLMToken("Hello from my provider.")


class MySTT(BaseSTT):
    async def transcribe(self, audio: AsyncIterator[bytes]) -> AsyncIterator[TranscriptEvent]:
        yield TranscriptEvent("example", True)


class MyTTS(BaseTTS):
    async def synthesize(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        async for _ in text:
            yield AudioChunk(b"\\x00\\x00"*2400, 24000, 1)
