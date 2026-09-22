from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("google.genai")

from voice_engine.core.types import ChatMessage
from voice_engine.providers.gemini import llm as gemini_llm
from voice_engine.providers.registry import create


def test_gemini_llm_registered():
    assert create("llm", "gemini", api_key="fake-key")


@pytest.mark.asyncio
async def test_generate_streams_tokens(monkeypatch):
    provider = gemini_llm.GeminiLLM(api_key="fake-key")

    async def fake_stream():
        for text in ["Hel", "lo", "!"]:
            yield MagicMock(text=text)

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = AsyncMock(return_value=fake_stream())
    monkeypatch.setattr(provider, "_get_client", lambda: fake_client)

    tokens = [t.text async for t in provider.generate([ChatMessage("user", "hi")])]

    assert tokens == ["Hel", "lo", "!"]
    fake_client.aio.models.generate_content_stream.assert_called_once()
