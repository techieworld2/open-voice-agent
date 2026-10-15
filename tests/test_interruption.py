import asyncio

import pytest

from voice_engine.core.agent import VoiceAgentPipeline
from voice_engine.core.audio import AudioProfile, speech_frame
from voice_engine.core.interruption import PlaybackTracker
from voice_engine.core.types import TranscriptEvent
from voice_engine.interfaces.stt import BaseSTT
from voice_engine.providers.mock.llm import MockLLM
from voice_engine.providers.mock.stt import MockSTT
from voice_engine.providers.mock.tts import MockTTS
from voice_engine.vad.energy import EnergyVAD


def test_playback_watermarks_do_not_count_unplayed_words():
    tracker = PlaybackTracker("g", 0, text="one two three four", sent_samples=1000)
    tracker.mark(250, 1)
    tracker.mark(500, 2)
    tracker.mark(1000, 4)
    tracker.acknowledge(600)
    assert tracker.spoken_text == "one two"
    assert tracker.discarded_text == "three four"
    tracker.acknowledge(2000)
    tracker.acknowledge(100)
    assert tracker.played_samples == 600


@pytest.mark.asyncio
async def test_history_truncates_to_rendered_words_and_flush_ack():
    events = []
    first_word = asyncio.Event()

    async def sink(event):
        events.append(event)
        if event.type == "audio.chunk" and event.data["end_sample"] >= 4800:
            first_word.set()

    pipeline = VoiceAgentPipeline(MockSTT(), MockLLM("one two three four."), MockTTS(), EnergyVAD(), sink)
    try:
        pipeline.start_response("hello")
        await asyncio.wait_for(first_word.wait(), 2)
        tracker = pipeline.playback
        await pipeline.acknowledge_playback(tracker.generation_id, 4800)
        await pipeline.interrupt()
        assert pipeline.session.messages[-1].role == "assistant"
        assert pipeline.session.messages[-1].content == "one"
        clear = next(event for event in events if event.type == "barge_in_clear")
        assert clear.data["spoken_text"] == "one"
        assert clear.data["discarded_text"] == "two three four."
        assert clear.data["barge_in_cancel_latency_ms"] >= 0
        clear_index = events.index(clear)
        await asyncio.sleep(.05)
        assert all(event.type != "audio.chunk" for event in events[clear_index + 1:])

        await pipeline.acknowledge_playback(tracker.generation_id, tracker.sent_samples, cleared=True)
        assert tracker.clear_ack.is_set()
        assert pipeline.session.messages[-1].content == tracker.spoken_text
    finally:
        await pipeline.close()


class FixedSTT(BaseSTT):
    def __init__(self, text, streaming=True):
        self.text, self.streaming = text, streaming

    async def transcribe(self, audio):
        count = 0
        async for _ in audio:
            count += 1
            if self.streaming and count == 5:
                yield TranscriptEvent(self.text, False)
        yield TranscriptEvent(self.text, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("text,frames,cancel", [("yeah", 25, False), ("stop please", 15, False), ("stop please", 16, True)])
async def test_pipeline_filters_backchannels_and_short_noise(text, frames, cancel):
    events = []

    async def sink(event):
        events.append(event.type)

    profile = AudioProfile.select("telephony")
    pipeline = VoiceAgentPipeline(FixedSTT(text), MockLLM(), MockTTS(),
                                  EnergyVAD(sample_rate=8000, min_silence_ms=350), sink, profile)
    try:
        pipeline.start_response("hello")
        await asyncio.sleep(.05)
        for index in range(frames):
            await pipeline.process_audio(speech_frame(profile, index))
            await asyncio.sleep(0)
        if not cancel:
            for _ in range(18):
                await pipeline.process_audio(bytes(320))
                await asyncio.sleep(0)
            if pipeline._stt_task:
                await pipeline._stt_task
        assert ("barge_in_clear" in events) == cancel
        if not cancel:
            assert "backchannel.ignored" in events
    finally:
        await pipeline.close()


@pytest.mark.asyncio
async def test_partial_transcript_extends_pipeline_silence():
    profile = AudioProfile.select("telephony")
    pipeline = VoiceAgentPipeline(FixedSTT("I wanted to go because"), MockLLM(), MockTTS(),
                                  EnergyVAD(sample_rate=8000, min_silence_ms=350), profile=profile)
    try:
        for i in range(10):
            await pipeline.process_audio(speech_frame(profile, i))
            await asyncio.sleep(0)
        assert pipeline.detector.silence_ms == 1100
        for _ in range(18):
            await pipeline.process_audio(bytes(320))
        assert pipeline.vad.is_speaking
        assert pipeline.session.task is None
        for _ in range(37):
            await pipeline.process_audio(bytes(320))
        assert not pipeline.vad.is_speaking
    finally:
        await pipeline.close()


@pytest.mark.asyncio
async def test_batch_final_transcript_delays_response_until_extended_deadline():
    profile = AudioProfile.select("telephony")
    pipeline = VoiceAgentPipeline(FixedSTT("I wanted to go because", streaming=False), MockLLM(), MockTTS(),
                                  EnergyVAD(sample_rate=8000, min_silence_ms=350), profile=profile)
    try:
        for i in range(10):
            await pipeline.process_audio(speech_frame(profile, i))
            await asyncio.sleep(0)
        for _ in range(18):
            await pipeline.process_audio(bytes(320))
        await pipeline._stt_task
        assert pipeline.detector.silence_ms == 1100
        await asyncio.sleep(.4)
        assert pipeline.session.task is None
        await asyncio.wait_for(pipeline._turn_task, 1)
        assert pipeline.session.task is not None
    finally:
        await pipeline.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["llm", "tts"])
async def test_provider_error_before_first_audio_does_not_hang(failed_stage):
    class BrokenLLM(MockLLM):
        async def generate(self, messages):
            raise RuntimeError("upstream unavailable")
            yield

    class BrokenTTS(MockTTS):
        async def synthesize(self, text):
            raise RuntimeError("synthesis unavailable")
            yield

    events = []

    async def sink(event):
        events.append(event)

    llm = BrokenLLM() if failed_stage == "llm" else MockLLM()
    tts = BrokenTTS() if failed_stage == "tts" else MockTTS()
    pipeline = VoiceAgentPipeline(MockSTT(), llm, tts, EnergyVAD(), sink)
    try:
        pipeline.start_response("hi")
        await asyncio.wait_for(pipeline.session.task, 1)
        errors = [event.data for event in events if event.type == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "PIPELINE_ERROR"
        assert errors[0]["stage"] == failed_stage
    finally:
        await pipeline.close()


@pytest.mark.asyncio
async def test_stage_events_explain_slow_recognition_and_follow_response():
    finished_recognition = asyncio.Event()
    processing = asyncio.Event()
    first_audio = asyncio.Event()
    events = []

    class DelayedSTT(BaseSTT):
        async def transcribe(self, audio):
            async for _ in audio:
                pass
            await finished_recognition.wait()
            yield TranscriptEvent("Hello there.", True)

    async def sink(event):
        events.append(event)
        if event.type == "pipeline.stage" and event.data["stage"] == "stt" and event.data["status"] == "processing":
            processing.set()
        if event.type == "audio.chunk":
            first_audio.set()

    pipeline = VoiceAgentPipeline(DelayedSTT(), MockLLM("Hello!"), MockTTS(), EnergyVAD(), sink)
    try:
        await pipeline.start_utterance()
        pipeline.feed_audio(bytes(640))
        await pipeline.end_utterance()
        await asyncio.wait_for(processing.wait(), 1)
        assert not any(event.type == "transcript" for event in events)
        assert not any(event.type == "assistant.token" for event in events)
        finished_recognition.set()
        await asyncio.wait_for(first_audio.wait(), 2)
        await pipeline.session.task
        stages = {(event.data["stage"], event.data["status"]) for event in events if event.type == "pipeline.stage"}
        assert {("stt", "processing"), ("stt", "completed"), ("llm", "processing"),
                ("llm", "completed"), ("tts", "streaming"), ("tts", "completed")} <= stages
    finally:
        await pipeline.close()
