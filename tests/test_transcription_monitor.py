import asyncio
import importlib
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from voice_engine.core.agent import VoiceAgentPipeline
from voice_engine.core.audio import AudioProfile, decode_audio, resample_pcm, speech_frame
from voice_engine.core.types import TranscriptEvent
from voice_engine.providers.mock.llm import MockLLM
from voice_engine.providers.mock.stt import MockSTT
from voice_engine.providers.mock.tts import MockTTS
from voice_engine.vad.energy import EnergyVAD


class QuotaLLM(MockLLM):
    async def generate(self, messages):
        assert all(message.content for message in messages)
        raise RuntimeError("429 quota reached")
        yield


@pytest.mark.asyncio
async def test_transcripts_precede_llm_quota_errors_and_next_utterance_still_works():
    class LocalSTT(MockSTT):
        async def transcribe(self, audio):
            async for _ in audio:
                pass
            yield TranscriptEvent("This transcription stays visible.", True)

    events = []

    async def sink(event):
        events.append(event)

    pipeline = VoiceAgentPipeline(LocalSTT(), QuotaLLM(), MockTTS(), EnergyVAD(), sink)
    try:
        for utterance in (1, 2):
            pipeline._last_voice_at = time.perf_counter() - 2
            await pipeline.start_utterance()
            pipeline.feed_audio(bytes(640))
            await pipeline.end_utterance()
            await pipeline._stt_task
            await pipeline._turn_task
            await asyncio.wait_for(pipeline.session.task, 1)
            final = next(event for event in events if event.type == "transcript" and event.data["utterance_id"] == utterance)
            errors = [event for event in events if event.type == "error"]
            assert len(errors) == utterance
            assert events.index(final) < events.index(errors[-1])
            assert final.data["text"] == "This transcription stays visible."
            assert errors[-1].data["stage"] == "llm"
            assert not pipeline.playback_active
        assert all(message.role == "user" for message in pipeline.session.messages)
    finally:
        await pipeline.close()


@pytest.mark.asyncio
async def test_transcription_only_never_calls_llm_or_tts():
    class ForbiddenLLM(MockLLM):
        async def generate(self, messages):
            pytest.fail("Transcription-only mode called LLM")
            yield

    events = []

    async def sink(event):
        events.append(event)

    pipeline = VoiceAgentPipeline(MockSTT(), ForbiddenLLM(), MockTTS(), EnergyVAD(), sink)
    try:
        await pipeline.configure_response(False)
        pipeline._last_voice_at = time.perf_counter() - 2
        await pipeline.start_utterance()
        pipeline.feed_audio(bytes(640))
        await pipeline.end_utterance()
        await pipeline._stt_task
        await pipeline._turn_task
        assert any(event.type == "transcript" and event.data["final"] for event in events)
        assert any(event.type == "turn.transcribed" for event in events)
        assert not any(event.type in {"audio.start", "assistant.token"} for event in events)
        assert pipeline.session.task is None
        pipeline.start_response("Even text cannot trigger generation while disabled")
        assert pipeline.session.task is None
        with pytest.raises(ValueError):
            await pipeline.configure_response("false")
        assert not pipeline.response_enabled
    finally:
        await pipeline.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,codec", [("wideband", "pcm_s16le"), ("telephony", "pcm_s16le"), ("telephony", "mulaw")])
async def test_input_monitor_echoes_exact_stt_pcm_without_recognition(mode, codec):
    profile = AudioProfile.select(mode, codec)
    events = []

    async def sink(event):
        events.append(event)

    pipeline = VoiceAgentPipeline(MockSTT(), MockLLM(), MockTTS(), EnergyVAD(sample_rate=profile.sample_rate), sink, profile)
    try:
        wire = speech_frame(profile)
        await pipeline.process_audio(wire)
        assert not any(event.type == "input.audio" for event in events)
        pipeline.monitor_input = True
        await pipeline.process_audio(wire)
        event = next(event for event in events if event.type == "input.audio")
        assert event.data["sample_rate"] == 16000
        assert event.data["encoding"] == "pcm_s16le"
        assert event.data["audio"] == resample_pcm(decode_audio(wire, codec), profile.sample_rate, 16000)
        assert len(event.data["audio"]) == 640
        assert pipeline.session.task is None
    finally:
        await pipeline.close()


def test_websocket_monitor_and_transcription_configuration(monkeypatch):
    server = importlib.import_module("voice_engine.server.app")
    monkeypatch.setattr(server, "settings", replace(server.settings, auth_token="", allowed_origins=("http://testserver",)))
    monkeypatch.setattr(server, "create", lambda kind, name: {"stt": MockSTT, "llm": QuotaLLM, "tts": MockTTS}[kind]())
    with TestClient(server.app) as client, client.websocket_connect("/ws/voice?mode=telephony", headers={"origin": "http://testserver"}) as socket:
        socket.send_json({"type": "session.configure", "response_enabled": False})
        assert socket.receive_json()["response_enabled"] is False
        socket.send_json({"type": "input.monitor", "enabled": "yes"})
        assert socket.receive_json()["code"] == "INVALID_MONITOR_CONFIG"
        socket.send_json({"type": "input.monitor", "enabled": True})
        assert socket.receive_json()["type"] == "input.monitor.ack"
        socket.send_bytes(bytes(320))
        assert socket.receive_json() == {"type": "input.audio", "sample_rate": 16000, "encoding": "pcm_s16le",
                                         "vad_speaking": False, "vad_revision": 0}
        assert socket.receive_bytes() == bytes(640)
        socket.send_json({"type": "input.monitor", "enabled": False})
        assert socket.receive_json()["enabled"] is False
        socket.send_json({"type": "session.end"})
