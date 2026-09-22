"""TTS abstraction."""

from abc import ABC, abstractmethod
from typing import AsyncIterator
from voice_engine.core.types import AudioChunk


class BaseTTS(ABC):
    """Streaming text-to-speech interface."""

    @abstractmethod
    async def synthesize(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        """Yield audio chunks from streamed text."""
        raise NotImplementedError

    async def cancel(self) -> None:
        """Cancel active synthesis if supported."""

    async def close(self) -> None:
        """Release resources."""
