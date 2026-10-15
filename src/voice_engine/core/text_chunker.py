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
            if stripped.endswith((".", "!", "?", ",", ";", ":")):
                if stripped:
                    yield stripped
                buffer = ""
            elif len(stripped) >= self.max_chars:

                boundary = buffer.rfind(" ", 0, self.max_chars + 1)
                if boundary > 0:
                    yield buffer[:boundary].strip()
                    buffer = buffer[boundary + 1:]
        if buffer.strip():
            yield buffer.strip()
