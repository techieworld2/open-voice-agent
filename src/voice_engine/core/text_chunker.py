"""Streaming LLM text chunking."""

from typing import AsyncIterator


class TextChunker:
    """Turn token streams into TTS-friendly phrases."""

    def __init__(self, max_chars: int = 100) -> None:
        self.max_chars = max_chars

    async def chunk(self, tokens: AsyncIterator[str]) -> AsyncIterator[str]:
        """Yield short phrase chunks as tokens arrive."""
        buffer = ""
        async for token in tokens:
            buffer += token
            stripped = buffer.rstrip()
            if (
                stripped.endswith((".", "!", "?", ",", ";", ":"))
                or len(stripped) >= self.max_chars
            ):
                if stripped:
                    yield stripped
                buffer = ""
        if buffer.strip():
            yield buffer.strip()
