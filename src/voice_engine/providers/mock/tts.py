"""Deterministic mock TTS."""

import asyncio
import math
import struct
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

            for index, _word in enumerate(phrase.split()):
                samples = 7200
                pcm = [int(1800 * math.sin(2 * math.pi * (420 + index * 30) * i / 24000)
                           * min(1, i / 120, (samples - i) / 120)) for i in range(samples)]
                yield AudioChunk(struct.pack("<" + "h" * samples, *pcm), 24000, 1, (samples,))
