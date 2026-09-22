from unittest.mock import MagicMock

import pytest

pytest.importorskip("faster_whisper")
pytest.importorskip("numpy")

from voice_engine.providers.faster_whisper import stt as fw_stt
from voice_engine.providers.registry import create


def test_faster_whisper_registered():
    assert create("stt", "faster_whisper")


async def _frames():
    yield b"\x00\x00" * 100
    yield b"\x00\x00" * 100


@pytest.mark.asyncio
async def test_transcribe_buffers_then_runs_once(monkeypatch):
    provider = fw_stt.FasterWhisperSTT()

    fake_segment = MagicMock(text=" hello world ", no_speech_prob=0.1, avg_logprob=-0.2)
    fake_model = MagicMock()
    fake_model.transcribe.return_value = ([fake_segment], None)
    monkeypatch.setattr(fw_stt, "_load_model", lambda *a: fake_model)

    events = [event async for event in provider.transcribe(_frames())]

    assert len(events) == 1
    assert events[0].text == "hello world"
    assert events[0].final is True
    fake_model.transcribe.assert_called_once()


@pytest.mark.asyncio
async def test_transcribe_drops_hallucinated_segments(monkeypatch):
    provider = fw_stt.FasterWhisperSTT()

    # Simulates a cough/sneeze/chair-scrape: loud enough to reach the decoder,
    # but Whisper itself is unsure it's speech and unconfident about the text.
    noise_segment = MagicMock(text=" Thanks for watching! ", no_speech_prob=0.9, avg_logprob=-1.8)
    fake_model = MagicMock()
    fake_model.transcribe.return_value = ([noise_segment], None)
    monkeypatch.setattr(fw_stt, "_load_model", lambda *a: fake_model)

    events = [event async for event in provider.transcribe(_frames())]

    assert events == []


@pytest.mark.asyncio
async def test_transcribe_empty_audio_yields_nothing():
    provider = fw_stt.FasterWhisperSTT()

    async def empty():
        return
        yield  # pragma: no cover

    events = [event async for event in provider.transcribe(empty())]
    assert events == []
