"""Deterministic mock TTS."""

import asyncio
from typing import AsyncIterator
from voice_engine.core.types import AudioChunk
from voice_engine.interfaces.tts import BaseTTS
from voice_engine.providers.registry import register


@register("tts", "mock")
class MockTTS(BaseTTS):
    """Emit placeholder PCM without external dependencies."""

    async def synthesize(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        async for phrase in text:
            await asyncio.sleep(0.005)
            samples = max(1920, min(12000, len(phrase) * 240))
            yield AudioChunk(b"\x00\x00" * samples, 24000, 1)
