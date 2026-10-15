from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor
import time

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
        yield

    events = [event async for event in provider.transcribe(empty())]
    assert events == []


def test_model_initialization_is_shared_across_worker_threads(monkeypatch):
    import faster_whisper

    model = object()

    def create_model(*args, **kwargs):

        time.sleep(.05)
        return model

    constructor = MagicMock(side_effect=create_model)
    monkeypatch.setattr(faster_whisper, "WhisperModel", constructor)
    monkeypatch.setattr(fw_stt, "_model_cache", {})
    with ThreadPoolExecutor(max_workers=4) as pool:
        models = list(pool.map(lambda _: fw_stt._load_model("base", "cpu", "int8"), range(4)))
    assert all(result is model for result in models)
    constructor.assert_called_once_with("base", device="cpu", compute_type="int8")


def test_failed_model_load_is_not_cached(monkeypatch):
    import faster_whisper

    model = object()
    constructor = MagicMock(side_effect=[RuntimeError("download failed"), model])
    monkeypatch.setattr(faster_whisper, "WhisperModel", constructor)
    monkeypatch.setattr(fw_stt, "_model_cache", {})
    with pytest.raises(RuntimeError, match="download failed"):
        fw_stt._load_model("base", "cpu", "int8")
    assert fw_stt._model_cache == {}
    assert fw_stt._load_model("base", "cpu", "int8") is model
