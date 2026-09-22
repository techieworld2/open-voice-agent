from unittest.mock import MagicMock

import pytest

pytest.importorskip("google.genai")

from voice_engine.providers.gemini import tts as gemini_tts
from voice_engine.providers.registry import create


def test_gemini_registered():
    assert create("tts", "gemini", api_key="fake-key")


async def _one_phrase():
    yield "Hello there."


@pytest.mark.asyncio
async def test_gemini_synthesize_uses_client(monkeypatch):
    provider = gemini_tts.GeminiTTS(api_key="fake-key")

    fake_part = MagicMock()
    fake_part.inline_data.data = b"\x01\x02"
    fake_response = MagicMock()
    fake_response.candidates = [MagicMock(content=MagicMock(parts=[fake_part]))]

    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response
    monkeypatch.setattr(provider, "_get_client", lambda: fake_client)

    chunks = [chunk async for chunk in provider.synthesize(_one_phrase())]

    assert len(chunks) == 1
    assert chunks[0].data == b"\x01\x02"
    assert chunks[0].sample_rate == 24000
    fake_client.models.generate_content.assert_called_once()
