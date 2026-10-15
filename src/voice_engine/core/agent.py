"""Realtime voice pipeline with semantic turns and acknowledged playback."""

import asyncio
from collections import deque
from contextlib import suppress
from copy import copy
from dataclasses import dataclass, field
import math
import logging
import time
from typing import AsyncIterator, Awaitable, Callable
import uuid

from voice_engine.core.audio import AudioProfile, decode_audio, encode_audio, resample_pcm
from voice_engine.core.interruption import PlaybackTracker
from voice_engine.core.text_chunker import TextChunker
from voice_engine.core.turn_detector import BackchannelGuard, TurnDetector
from voice_engine.core.types import ChatMessage, PipelineEvent, VoiceState
from voice_engine.interfaces.llm import BaseLLM
from voice_engine.interfaces.stt import BaseSTT
from voice_engine.interfaces.tts import BaseTTS
from voice_engine.observability.metrics import AUDIO_CHUNKS, ERRORS, INTERRUPTIONS, TTFB
from voice_engine.vad.energy import EnergyVAD

logger = logging.getLogger("voice_engine.pipeline")


@dataclass
class Session:
    session_id: str
    state: VoiceState = VoiceState.IDLE
    generation_id: str | None = None
    task: asyncio.Task[None] | None = None
    messages: list[ChatMessage] = field(default_factory=list)


class VoiceAgentPipeline:
    def __init__(self, stt: BaseSTT, llm: BaseLLM, tts: BaseTTS, vad: EnergyVAD,
                 event_sink: Callable[[PipelineEvent], Awaitable[None]] | None = None,
                 profile: AudioProfile | None = None) -> None:
        self.stt, self.llm, self.tts, self.vad = stt, llm, tts, vad
        self.event_sink = event_sink
        self.profile = profile or AudioProfile.select()
        self.detector = TurnDetector(self.profile)
        self.guard = BackchannelGuard()
        self.session = Session(str(uuid.uuid4()))
        self.chunker = TextChunker()
        self.playback: PlaybackTracker | None = None
        self._playbacks: dict[str, PlaybackTracker] = {}
        self._interrupt_lock = asyncio.Lock()
        self._closed = False
        self._audio_queue: asyncio.Queue[bytes | None] | None = None
        self._stt_task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._retired_tasks: set[asyncio.Task] = set()
        self._candidate = False
        self._voiced_ms = 0
        self._barge_threshold_at: float | None = None
        self._transcript = ""
        self._pending_text = ""
        self._preroll: deque[bytes] = deque(maxlen=max(1, vad.min_speech_frames))
        self._utterance_frames = 0
        self._utterance_id = 0
        self._last_voice_at = time.perf_counter()
        self._turn_end_at = self._last_voice_at
        self.response_enabled = True
        self.monitor_input = False
        self.vad_revision = 0
        self._vad_frame_count = 0
        self._turn_window_changed = asyncio.Event()

    async def emit(self, event_type: str, **data: object) -> None:
        if event_type == "pipeline.stage":
            logger.info("pipeline.stage session=%s stage=%s status=%s utterance=%s generation=%s",
                        self.session.session_id, data.get("stage"), data.get("status"),
                        data.get("utterance_id"), data.get("generation_id"))
        elif event_type == "transcript":
            logger.info("transcript session=%s final=%s characters=%s", self.session.session_id,
                        data.get("final"), len(str(data.get("text", ""))))
        elif event_type == "error":
            logger.error("pipeline.error session=%s code=%s message=%s", self.session.session_id,
                         data.get("code"), data.get("message"))
        if self.event_sink:
            await self.event_sink(PipelineEvent(event_type, data))

    async def _settle(self, task: asyncio.Task, timeout: float = .25) -> None:
        """A provider that swallows cancellation cannot hold the transport open."""
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            with suppress(asyncio.CancelledError, Exception):
                await task
        else:
            self._retired_tasks.add(task)

            def completed(retired: asyncio.Task) -> None:
                self._retired_tasks.discard(retired)
                with suppress(asyncio.CancelledError, Exception):
                    retired.exception()

            task.add_done_callback(completed)

    @property
    def playback_active(self) -> bool:
        return bool((self.session.task and not self.session.task.done()) or
                    (self.playback and not self.playback.interrupted and
                     self.playback.played_samples < self.playback.sent_samples))

    async def _context_sync(self, tracker: PlaybackTracker | None = None) -> None:
        tracker = tracker or self.playback
        if tracker and tracker.interrupted:
            self.session.messages[tracker.message_index] = ChatMessage("assistant", tracker.spoken_text)
            await self.emit("context.sync", generation_id=tracker.generation_id,
                            spoken_text=tracker.spoken_text, discarded_text=tracker.discarded_text,
                            precision=tracker.precision)

    async def acknowledge_playback(self, generation: str, samples: int, cleared: bool = False) -> None:
        tracker = self._playbacks.get(generation)
        if tracker and tracker.generation_id == generation:
            tracker.acknowledge(samples)
            if cleared:
                tracker.clear_ack.set()
            await self._context_sync(tracker)

    async def interrupt(self, threshold_at: float | None = None) -> None:
        started = threshold_at or time.perf_counter()
        async with self._interrupt_lock:
            tracker = self.playback
            generation = self.session.generation_id or (tracker.generation_id if tracker else None)
            self.session.generation_id = None
            task = self.session.task
            if task and not task.done():
                INTERRUPTIONS.inc()
                task.cancel()
            if tracker:
                tracker.interrupted = True
            self.session.state = VoiceState.INTERRUPTED


            latency = (time.perf_counter() - started) * 1000
            await self.emit("barge_in_clear", generation_id=generation,
                            barge_in_cancel_latency_ms=latency,
                            spoken_text=tracker.spoken_text if tracker else "",
                            discarded_text=tracker.discarded_text if tracker else "",
                            precision=tracker.precision if tracker else "phrase")
            await self.emit("CLEAR_BUFFER", generation_id=generation)
            if task:
                await self._settle(task)
            await self._settle(asyncio.create_task(self.tts.cancel()))
            self.session.task = None
            await self._context_sync()
            await self.emit("metrics", barge_in_cancel_latency_ms=latency,
                            barge_in_cleanup_latency_ms=(time.perf_counter() - started) * 1000)
            self.session.state = VoiceState.LISTENING

    async def _semantic_update(self, text: str) -> None:
        self.detector.update(text)
        self.vad.configure(min_silence_ms=self.detector.silence_ms)
        self._turn_window_changed.set()
        await self.emit("turn.window", silence_ms=self.detector.silence_ms, reason=self.detector.reason)

    def configure_vad(self, *, speech_threshold=None, silence_threshold=None,
                      min_speech_ms=None, min_silence_ms=None, semantic_eot=None,
                      trailing_silence_ms=None, barge_in_min_speech_ms=None,
                      filter_backchannels=None) -> None:
        """Apply lab controls atomically; semantic updates retain selected durations."""
        for value in (semantic_eot, filter_backchannels):
            if value is not None and not isinstance(value, bool):
                raise ValueError("Semantic EoT and backchannel filtering must be booleans")
        for name, value, minimum in (("min_silence_ms", min_silence_ms, 20),
                                     ("trailing_silence_ms", trailing_silence_ms, 20),
                                     ("barge_in_min_speech_ms", barge_in_min_speech_ms, 320)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or not minimum <= value <= 5000):
                raise ValueError(f"{name} must be a finite number from {minimum} to 5000")
        detector, guard, vad = copy(self.detector), copy(self.guard), copy(self.vad)
        if semantic_eot is not None:
            detector.semantic_enabled = semantic_eot
        if min_silence_ms is not None:
            detector.base_silence_ms = min_silence_ms
        if trailing_silence_ms is not None:
            detector.trailing_silence_ms = trailing_silence_ms
        if barge_in_min_speech_ms is not None:
            guard.min_speech_ms = barge_in_min_speech_ms
        if filter_backchannels is not None:
            guard.filter_backchannels = filter_backchannels
        vad.configure(speech_threshold=speech_threshold, silence_threshold=silence_threshold,
                      min_speech_ms=min_speech_ms, min_silence_ms=detector.silence_ms)
        self.detector, self.guard, self.vad = detector, guard, vad
        self._preroll = deque(self._preroll, maxlen=vad.min_speech_frames)
        self.vad_revision += 1
        self._turn_window_changed.set()
        if self._voiced_ms < guard.min_speech_ms:
            self._barge_threshold_at = None

    async def _confirm_candidate(self, final: bool = False) -> bool:
        if self._candidate and self.guard.confirm(self._voiced_ms, self._transcript, final):
            self._candidate = False
            await self.interrupt(self._barge_threshold_at)
            return True
        return False

    async def start_utterance(self) -> None:
        self._utterance_id += 1
        utterance_id = self._utterance_id
        if self._turn_task:
            self._turn_task.cancel()
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
            await self._settle(self._stt_task)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1600)
        self._audio_queue = queue
        self._transcript = ""
        self._utterance_frames = 0

        async def audio_stream() -> AsyncIterator[bytes]:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                yield chunk

        async def run() -> None:
            final_text = ""
            try:
                async for event in self.stt.transcribe(audio_stream()):
                    if self._closed or utterance_id != self._utterance_id:
                        return
                    self._transcript = event.text
                    await self.emit("transcript", text=event.text, final=event.final, utterance_id=utterance_id)
                    await self._semantic_update(event.text)
                    await self._confirm_candidate(event.final)
                    if event.final:
                        final_text = event.text.strip()
                if self._closed:
                    return
                await self.emit("pipeline.stage", stage="stt", status="completed" if final_text else "empty",
                                utterance_id=utterance_id,
                                detail="Speech recognized." if final_text else "No speech text was recognized.")
                if self._candidate:
                    await self.emit("backchannel.ignored", text=final_text, voiced_ms=self._voiced_ms)
                    self._candidate = False
                    self.detector.update("")
                    self.vad.configure(min_silence_ms=self.detector.silence_ms)
                    return
                if final_text:
                    self._pending_text = " ".join(filter(None, (self._pending_text, final_text)))
                    self._turn_task = asyncio.create_task(self._finish_turn())
            except asyncio.CancelledError:
                if not self._closed:
                    await self.emit("pipeline.stage", stage="stt", status="cancelled", utterance_id=utterance_id,
                                    detail="Recognition superseded by a new speech burst.")
                raise
            except Exception as exc:
                ERRORS.inc()
                await self.emit("error", code="STT_ERROR", stage="stt", message=str(exc))

        await self.emit("pipeline.stage", stage="stt", status="capturing", utterance_id=utterance_id)
        self._stt_task = asyncio.create_task(run())

    def feed_audio(self, frame: bytes) -> None:
        if self._audio_queue is not None:
            self._audio_queue.put_nowait(resample_pcm(frame, self.profile.sample_rate, 16000))

    async def end_utterance(self) -> None:
        if self._audio_queue is not None:
            self._audio_queue.put_nowait(None)
            self._audio_queue = None
            await self.emit("pipeline.stage", stage="stt", status="processing", utterance_id=self._utterance_id,
                            detail="Recognizing speech; first use may download/load the STT model.",
                            model=getattr(self.stt, "model_size", None))

    async def _finish_turn(self) -> None:

        while True:
            self._turn_window_changed.clear()
            remaining = self.detector.silence_ms / 1000 - (time.perf_counter() - self._last_voice_at)
            if remaining <= 0:
                break
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._turn_window_changed.wait(), remaining)
        text, self._pending_text = self._pending_text, ""
        if self.response_enabled:
            self.start_response(text, turn_ended_at=self._last_voice_at)
        else:
            await self.emit("turn.transcribed", text=text, utterance_id=self._utterance_id)

    async def configure_response(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("response_enabled must be a boolean")
        self.response_enabled = enabled
        if not enabled and self.playback_active:
            await self.interrupt()

    async def process_audio(self, wire_frame: bytes) -> None:
        if len(wire_frame) != self.profile.frame_bytes:
            raise ValueError(f"Expected exactly {self.profile.frame_bytes} bytes per 20ms frame")
        frame = decode_audio(wire_frame, self.profile.encoding)
        level = self.vad.rms(frame)
        voiced = level >= (self.vad.silence_threshold if self.vad.is_speaking else self.vad.speech_threshold)
        if voiced:
            self._last_voice_at = time.perf_counter()
        self._preroll.append(frame)
        events = self.vad.process(frame)
        started = any(e.type == "speech_started" for e in events)
        ended = any(e.type == "speech_ended" for e in events)
        if self.monitor_input:


            await self.emit("input.audio", audio=resample_pcm(frame, self.profile.sample_rate, 16000),
                            sample_rate=16000, encoding="pcm_s16le",
                            vad_speaking=self.vad.is_speaking, vad_revision=self.vad_revision)
        self._vad_frame_count += 1
        if started or ended or self._vad_frame_count % 5 == 0:
            await self.emit("vad.status", rms=level, speaking=self.vad.is_speaking,
                            silence_ms=self.vad.silence_elapsed_ms,
                            effective_silence_ms=self.vad.min_silence_frames * self.profile.frame_ms,
                            revision=self.vad_revision)
        if started:
            self._candidate = self.playback_active
            self._voiced_ms = len(self._preroll) * self.profile.frame_ms
            self._barge_threshold_at = time.perf_counter() if self._voiced_ms >= self.guard.min_speech_ms else None
            await self.start_utterance()
            await self.emit("speech_started")
            for buffered in self._preroll:
                self.feed_audio(buffered)
        elif self.vad.is_speaking or ended:
            self._voiced_ms += self.profile.frame_ms if voiced else 0
            self.feed_audio(frame)
        if self._candidate and self._voiced_ms >= self.guard.min_speech_ms and self._barge_threshold_at is None:
            self._barge_threshold_at = time.perf_counter()
        await self._confirm_candidate()
        if self._audio_queue is not None:
            self._utterance_frames += 1
            if self._utterance_frames > 1500:
                raise ValueError("Utterance exceeds 30 seconds; send silence between turns")
        if ended:
            await self.emit("speech_ended")
            await self.end_utterance()
            self._preroll.clear()

    async def handle_vad_event(self, event_type: str) -> None:
        """Compatibility hook for callers with their own confirmed VAD decisions."""
        await self.emit(event_type)
        if event_type == "speech_started":
            await self.interrupt()

    async def run_response(self, user_text: str) -> None:
        turn_ended_at = self._turn_end_at
        previous = self.playback
        if previous and previous.interrupted and not previous.clear_ack.is_set():


            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(previous.clear_ack.wait(), 0.25)
        generation = str(uuid.uuid4())
        self.session.generation_id = generation
        self.session.state = VoiceState.THINKING
        self.session.messages.append(ChatMessage("user", user_text))
        history = list(self.session.messages)
        index = len(self.session.messages)
        self.session.messages.append(ChatMessage("assistant", ""))
        tracker = PlaybackTracker(generation, index)
        self.playback = tracker
        self._playbacks[generation] = tracker
        if len(self._playbacks) > 4:
            self._playbacks.pop(next(iter(self._playbacks)))
        started = time.perf_counter()
        first_audio = True
        pace_at: float | None = None
        phrases: asyncio.Queue[str | None] = asyncio.Queue(maxsize=4)

        async def token_stream() -> AsyncIterator[str]:
            first_token = True
            async for token in self.llm.generate(history):
                if generation != self.session.generation_id or tracker.interrupted:
                    raise asyncio.CancelledError
                if first_token:
                    first_token = False
                    await self.emit("pipeline.stage", stage="llm", status="streaming", generation_id=generation)
                tracker.text += token.text
                self.session.messages[index] = ChatMessage("assistant", tracker.text.strip())
                await self.emit("assistant.token", generation_id=generation, text=token.text)
                yield token.text

        async def produce() -> None:
            await self.emit("pipeline.stage", stage="llm", status="processing", generation_id=generation)
            async for phrase in self.chunker.chunk(token_stream()):
                await phrases.put(phrase)
            await self.emit("pipeline.stage", stage="llm", status="completed", generation_id=generation)
            await phrases.put(None)

        producer = asyncio.create_task(produce())
        spoken_words = 0
        tts_started = False
        try:
            self.session.state = VoiceState.SPEAKING
            await self.emit("audio.start", generation_id=generation)
            while True:
                get_phrase = asyncio.create_task(phrases.get())
                try:
                    done, _ = await asyncio.wait({get_phrase, producer}, return_when=asyncio.FIRST_COMPLETED)
                    if producer in done and producer.exception():
                        raise producer.exception()
                    phrase = await get_phrase
                finally:
                    if not get_phrase.done():
                        get_phrase.cancel()
                        with suppress(asyncio.CancelledError):
                            await get_phrase
                if phrase is None:
                    break
                if not tts_started:
                    tts_started = True
                    await self.emit("pipeline.stage", stage="tts", status="processing", generation_id=generation)
                phrase_words = len(phrase.split())
                phrase_start = tracker.sent_samples
                aligned_words = 0
                pending = bytearray()
                pending_marks: list[tuple[int, int]] = []
                converted_samples = 0

                async def one_phrase() -> AsyncIterator[str]:
                    yield phrase

                async def send_frame(pcm: bytes, valid_samples: int) -> None:
                    nonlocal first_audio, pace_at
                    if generation != self.session.generation_id:
                        raise asyncio.CancelledError
                    now = time.perf_counter()
                    if pace_at is not None and pace_at > now:
                        await asyncio.sleep(pace_at - now)
                    now = time.perf_counter()
                    pace_at = max(pace_at or now, now) + self.profile.frame_ms / 1000
                    offset = tracker.sent_samples
                    tracker.sent_samples += self.profile.samples_per_frame
                    for sample, count in pending_marks:
                        if offset < sample <= tracker.sent_samples:
                            tracker.mark(sample, count)
                    if first_audio:
                        first_audio = False
                        await self.emit("pipeline.stage", stage="tts", status="streaming", generation_id=generation)
                        ttfa = (now - turn_ended_at) * 1000
                        TTFB.observe(now - started)
                        await self.emit("metrics", ttfa_ms=ttfa,
                                        generation_to_first_audio_ms=(now - started) * 1000)
                    AUDIO_CHUNKS.inc()
                    await self.emit("audio.chunk", generation_id=generation,
                                    audio=encode_audio(pcm, self.profile.encoding),
                                    sample_rate=self.profile.sample_rate, channels=1,
                                    encoding=self.profile.encoding, start_sample=offset,
                                    end_sample=tracker.sent_samples, valid_samples=valid_samples)

                async for chunk in self.tts.synthesize(one_phrase()):
                    converted = resample_pcm(chunk.data, chunk.sample_rate, self.profile.sample_rate, chunk.channels)
                    chunk_samples = len(chunk.data) // (2 * chunk.channels)
                    previous_mark = 0
                    for mark in chunk.word_end_samples:
                        if isinstance(mark, bool) or not isinstance(mark, int) or mark <= previous_mark or mark > chunk_samples:
                            raise ValueError("Invalid TTS word alignment")
                        previous_mark = mark
                        aligned_words += 1
                        if aligned_words > phrase_words:
                            raise ValueError("TTS alignment contains more words than its phrase")
                        sample = phrase_start + converted_samples + math.ceil(mark * self.profile.sample_rate / chunk.sample_rate)
                        pending_marks.append((sample, spoken_words + aligned_words))
                        tracker.precision = "word"
                    converted_samples += len(converted) // 2
                    pending.extend(converted)
                    size = self.profile.samples_per_frame * 2
                    while len(pending) >= size:
                        pcm = bytes(pending[:size])
                        del pending[:size]
                        await send_frame(pcm, self.profile.samples_per_frame)
                if pending:
                    count = len(pending) // 2
                    pcm = bytes(pending).ljust(self.profile.samples_per_frame * 2, b"\0")
                    await send_frame(pcm, count)
                spoken_words += phrase_words
                if converted_samples:
                    tracker.mark(phrase_start + converted_samples, spoken_words)
                await self.emit("audio.phrase_end", generation_id=generation,
                                end_sample=phrase_start + converted_samples, word_count=spoken_words)
            if pace_at:
                await asyncio.sleep(max(0, pace_at - time.perf_counter()))
            await self.emit("pipeline.stage", stage="tts", status="completed", generation_id=generation)
            await self.emit("audio.end", generation_id=generation, end_sample=tracker.sent_samples,
                            end_to_end_turn_latency_ms=(time.perf_counter() - turn_ended_at) * 1000)
        except asyncio.CancelledError:
            await self.emit("generation.cancelled", generation_id=generation)
            raise
        except Exception as exc:
            ERRORS.inc()
            failed_stage = "llm" if producer.done() and not producer.cancelled() and producer.exception() else "tts"
            await self.emit("error", code="PIPELINE_ERROR", stage=failed_stage,
                            message=str(exc), generation_id=generation)


            if not tracker.text:
                self.session.messages.pop(index)
                self._playbacks.pop(generation, None)
                if self.playback is tracker:
                    self.playback = None
        finally:
            producer.cancel()
            await self._settle(producer)
            if self.session.generation_id == generation:
                self.session.generation_id = None
                self.session.state = VoiceState.LISTENING

    def start_response(self, text: str, turn_ended_at: float | None = None) -> None:
        if not self.response_enabled:
            return
        self._turn_end_at = turn_ended_at if turn_ended_at is not None else time.perf_counter()
        if self.session.task and not self.session.task.done():
            self.session.task.cancel()
        self.session.task = asyncio.create_task(self.run_response(text))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.session.generation_id = None
        self.session.state = VoiceState.DISCONNECTED
        tasks = [t for t in (self.session.task, self._stt_task, self._turn_task) if t] + list(self._retired_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            await self._settle(task)
        for cleanup in (self.tts.cancel, self.stt.close, self.llm.close, self.tts.close):
            await self._settle(asyncio.create_task(cleanup()))
