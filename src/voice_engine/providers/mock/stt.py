"""Deterministic mock STT."""

from typing import AsyncIterator
from voice_engine.core.types import TranscriptEvent
from voice_engine.interfaces.stt import BaseSTT
from voice_engine.providers.registry import register


@register("stt", "mock")
class MockSTT(BaseSTT):
    """Return a deterministic transcript after consuming audio."""

    async def transcribe(self, audio: AsyncIterator[bytes]) -> AsyncIterator[TranscriptEvent]:
        async for _ in audio:
            pass
        yield TranscriptEvent("Hello from the mock voice engine.", True)
