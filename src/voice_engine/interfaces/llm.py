"""LLM abstraction."""

from abc import ABC, abstractmethod
from typing import AsyncIterator
from voice_engine.core.types import ChatMessage, LLMToken


class BaseLLM(ABC):
    """Streaming language model interface."""

    @abstractmethod
    async def generate(self, messages: list[ChatMessage]) -> AsyncIterator[LLMToken]:
        """Yield assistant tokens."""
        raise NotImplementedError

    async def close(self) -> None:
        """Release resources."""
