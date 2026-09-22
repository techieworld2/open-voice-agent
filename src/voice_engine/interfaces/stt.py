"""STT abstraction."""

from abc import ABC, abstractmethod
from typing import AsyncIterator
from voice_engine.core.types import TranscriptEvent


class BaseSTT(ABC):
    """Streaming speech recognition interface."""

    @abstractmethod
    async def transcribe(self, audio: AsyncIterator[bytes]) -> AsyncIterator[TranscriptEvent]:
        """Consume PCM and yield transcript events."""
        raise NotImplementedError

    async def close(self) -> None:
        """Release resources."""
