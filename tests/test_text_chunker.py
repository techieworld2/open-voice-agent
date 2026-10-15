import pytest

from voice_engine.core.text_chunker import TextChunker


@pytest.mark.asyncio
async def test_chunker_preserves_words_across_token_and_size_boundaries():
    text = "hello beautiful wonderful streaming world"

    async def tokens():
        for piece in ["hello be", "autiful won", "derful stream", "ing world"]:
            yield piece

    phrases = [phrase async for phrase in TextChunker(max_chars=12).chunk(tokens())]
    assert " ".join(phrases) == text
