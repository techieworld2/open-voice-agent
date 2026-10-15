import asyncio
import math
import time

import pytest

from voice_engine.core.agent import VoiceAgentPipeline
from voice_engine.core.audio import speech_frame
from voice_engine.providers.mock.llm import MockLLM
from voice_engine.providers.mock.stt import MockSTT
from voice_engine.providers.mock.tts import MockTTS
from voice_engine.vad.energy import EnergyVAD


def pipeline():
    return VoiceAgentPipeline(MockSTT(), MockLLM(), MockTTS(), EnergyVAD())


@pytest.mark.asyncio
async def test_custom_silence_windows_survive_transcript_updates():
    agent = pipeline()
    try:
        agent.configure_vad(min_silence_ms=500, trailing_silence_ms=1400, min_speech_ms=180)
        await agent._semantic_update("I want to go because")
        assert agent.detector.silence_ms == 1400
        assert agent.vad.min_silence_frames == 70
        await agent._semantic_update("I want to go home.")
        assert agent.detector.silence_ms == 500
        assert agent.vad.min_silence_frames == 25
        assert agent._preroll.maxlen == 9
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_manual_eot_and_backchannel_controls():
    agent = pipeline()
    try:
        agent.configure_vad(semantic_eot=False, min_silence_ms=600, filter_backchannels=False,
                            barge_in_min_speech_ms=400)
        await agent._semantic_update("I would like to go because")
        assert agent.detector.silence_ms == 600
        assert agent.detector.reason == "Manual"
        assert not agent.guard.confirm(380, "please stop")
        assert agent.guard.confirm(400, "yeah")
    finally:
        await agent.close()


@pytest.mark.parametrize("data", [{"semantic_eot": "false"}, {"filter_backchannels": 1},
                                 {"barge_in_min_speech_ms": 200}, {"trailing_silence_ms": math.nan},
                                 {"min_silence_ms": -1}, {"min_speech_ms": "bad"}])
def test_invalid_vad_changes_are_atomic(data):
    agent = pipeline()
    previous = agent.vad, agent.detector, agent.guard
    with pytest.raises(ValueError):
        agent.configure_vad(speech_threshold=.1, **data)
    assert (agent.vad, agent.detector, agent.guard) == previous
    assert agent.vad.speech_threshold == .02


def test_trailing_window_cannot_shorten_selected_base_silence():
    agent = pipeline()
    agent.configure_vad(min_silence_ms=1500, trailing_silence_ms=1000)
    agent.detector.update("and")
    assert agent.detector.silence_ms == 1500


@pytest.mark.asyncio
async def test_live_threshold_and_duration_changes_affect_current_audio_without_restarting():
    events = []

    async def sink(event):
        events.append(event)

    agent = pipeline()
    agent.event_sink = sink
    agent.monitor_input = True
    session_id = agent.session.session_id
    try:
        agent.configure_vad(speech_threshold=.5, silence_threshold=.4, min_speech_ms=40)
        tone = speech_frame(agent.profile)
        for _ in range(10):
            await agent.process_audio(tone)
        assert not agent.vad.is_speaking
        assert not any(event.type == "speech_started" for event in events)
        assert events[-1].type == "vad.status"
        assert not events[-1].data["speaking"]

        agent.configure_vad(speech_threshold=.02, silence_threshold=.01, min_speech_ms=40,
                            min_silence_ms=1000, semantic_eot=False)
        await agent.process_audio(tone)
        assert not agent.vad.is_speaking
        await agent.process_audio(tone)
        assert agent.vad.is_speaking
        for _ in range(10):
            await agent.process_audio(bytes(640))
        assert agent.vad.is_speaking

        agent.configure_vad(min_silence_ms=100)
        await agent.process_audio(bytes(640))
        assert not agent.vad.is_speaking
        assert any(event.type == "speech_ended" for event in events)
        monitor = [event for event in events if event.type == "input.audio"]
        assert monitor[9].data["audio"] == monitor[10].data["audio"] == tone
        assert not monitor[9].data["vad_speaking"]
        assert monitor[11].data["vad_speaking"]
        assert not monitor[-1].data["vad_speaking"]
        assert monitor[-1].data["vad_revision"] == 3
        assert agent.session.session_id == session_id
    finally:
        await agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("extend_first", [False, True])
async def test_live_silence_updates_reschedule_already_pending_semantic_deadline(extend_first):
    agent = pipeline()
    agent.response_enabled = False
    agent._pending_text = "A completed transcription."
    agent._last_voice_at = time.perf_counter()
    agent.configure_vad(min_silence_ms=400, semantic_eot=False)
    pending = asyncio.create_task(agent._finish_turn())
    try:
        await asyncio.sleep(0)
        if extend_first:
            agent.configure_vad(min_silence_ms=1000)
            await asyncio.sleep(.05)
            assert not pending.done()
        agent.configure_vad(min_silence_ms=20)
        await asyncio.wait_for(pending, .2)
        assert agent._pending_text == ""
        assert agent.session.task is None
    finally:
        pending.cancel()
        await agent.close()
