from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("requests")

from voice_engine.providers.deepgram import tts as deepgram_tts
from voice_engine.providers.registry import create


def test_deepgram_registered():
    assert create("tts", "deepgram", api_key="fake-key")


async def _one_phrase():
    yield "Hello there."


@pytest.mark.asyncio
async def test_deepgram_synthesize_calls_api():
    provider = deepgram_tts.DeepgramTTS(api_key="fake-key")

    fake_response = MagicMock()
    fake_response.content = b"\x01\x02\x03\x04"
    fake_response.raise_for_status = MagicMock()

    with patch("requests.post", return_value=fake_response) as post:
        chunks = [chunk async for chunk in provider.synthesize(_one_phrase())]

    assert len(chunks) == 1
    assert chunks[0].data == b"\x01\x02\x03\x04"
    assert chunks[0].sample_rate == 24000
    post.assert_called_once()
    assert post.call_args.kwargs["headers"]["Authorization"] == "Token fake-key"


def test_deepgram_requires_api_key(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    provider = deepgram_tts.DeepgramTTS()
    with pytest.raises(RuntimeError):
        provider._synthesize_blocking("hi")
