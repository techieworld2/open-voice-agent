import asyncio
import pytest
from voice_engine.core.agent import VoiceAgentPipeline
from voice_engine.providers.mock.stt import MockSTT
from voice_engine.providers.mock.llm import MockLLM
from voice_engine.providers.mock.tts import MockTTS
from voice_engine.vad.energy import EnergyVAD


@pytest.mark.asyncio
async def test_interrupt_emits_clear_buffer():
    events=[]
    async def sink(event): events.append(event.type)
    p=VoiceAgentPipeline(MockSTT(),MockLLM(),MockTTS(),EnergyVAD(),sink)
    p.start_response("hello")
    await asyncio.sleep(.03)
    await p.interrupt()
    assert "CLEAR_BUFFER" in events
    await p.close()


@pytest.mark.asyncio
async def test_speech_started_clears_buffer_even_after_generation_finished():
    """Regression test: a short reply's generation can finish server-side
    (state back to LISTENING) well before the client finishes playing the
    audio out loud. Barging in during that trailing-playback window must
    still clear the client's buffer, even though there's no active task
    left to cancel."""
    events = []
    async def sink(event): events.append(event.type)
    p = VoiceAgentPipeline(MockSTT(), MockLLM(), MockTTS(), EnergyVAD(), sink)
    p.start_response("hello")
    await p.session.task  # let the (fast, mocked) generation run to completion
    assert p.session.state.value == "listening"

    events.clear()
    await p.handle_vad_event("speech_started")
    assert "CLEAR_BUFFER" in events
    await p.close()
