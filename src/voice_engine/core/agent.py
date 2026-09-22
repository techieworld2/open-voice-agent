"""Realtime voice pipeline."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, AsyncIterator

from voice_engine.core.text_chunker import TextChunker
from voice_engine.core.types import ChatMessage, PipelineEvent, VoiceState
from voice_engine.interfaces.llm import BaseLLM
from voice_engine.interfaces.stt import BaseSTT
from voice_engine.interfaces.tts import BaseTTS
from voice_engine.observability.metrics import AUDIO_CHUNKS, ERRORS, INTERRUPTIONS, TTFB
from voice_engine.vad.energy import EnergyVAD


@dataclass
class Session:
    """Mutable per-connection session."""

    session_id: str
    state: VoiceState = VoiceState.IDLE
    generation_id: str | None = None
    task: asyncio.Task[None] | None = None
    messages: list[ChatMessage] = field(default_factory=list)


class VoiceAgentPipeline:
    """Coordinate VAD, STT, LLM, TTS and interruption."""

    def __init__(
        self,
        stt: BaseSTT,
        llm: BaseLLM,
        tts: BaseTTS,
        vad: EnergyVAD,
        event_sink: Callable[[PipelineEvent], Awaitable[None]] | None = None,
    ) -> None:
        self.stt, self.llm, self.tts, self.vad = stt, llm, tts, vad
        self.event_sink = event_sink
        self.session = Session(str(uuid.uuid4()))
        self._interrupt_lock = asyncio.Lock()
        self._closed = False
        self._response_started: dict[str, float] = {}
        self._first_audio_seen: set[str] = set()
        self.chunker = TextChunker()
        self._audio_queue: "asyncio.Queue[bytes | None] | None" = None
        self._stt_task: asyncio.Task[None] | None = None

    async def emit(self, event_type: str, **data: object) -> None:
        """Emit an event to the transport."""
        if self.event_sink:
            await self.event_sink(PipelineEvent(event_type, data))

    async def interrupt(self) -> None:
        """Idempotently cancel the active generation and flush state."""
        async with self._interrupt_lock:
            task = self.session.task
            generation = self.session.generation_id
            if task and not task.done():
                INTERRUPTIONS.inc()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await self.tts.cancel()
            self.session.task = None
            self.session.generation_id = None
            self.session.state = VoiceState.INTERRUPTED
            await self.emit("CLEAR_BUFFER", generation_id=generation)
            self.session.state = VoiceState.LISTENING

    async def start_utterance(self) -> None:
        """Begin buffering mic audio for one STT pass."""
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
        queue: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        self._audio_queue = queue

        async def audio_stream() -> AsyncIterator[bytes]:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                yield chunk

        async def run() -> None:
            async for event in self.stt.transcribe(audio_stream()):
                await self.emit("transcript", text=event.text, final=event.final)
                if event.final and event.text.strip():
                    self.start_response(event.text)

        self._stt_task = asyncio.create_task(run())

    def feed_audio(self, frame: bytes) -> None:
        """Queue one PCM frame for the active utterance, if any."""
        if self._audio_queue is not None:
            self._audio_queue.put_nowait(frame)

    async def end_utterance(self) -> None:
        """Signal end of audio for the active utterance."""
        if self._audio_queue is not None:
            self._audio_queue.put_nowait(None)
            self._audio_queue = None

    async def handle_vad_event(self, event_type: str) -> None:
        """React to VAD events."""
        await self.emit(event_type)
        if event_type == "speech_started":
            # No-op if nothing's active, so this also clears trailing client playback.
            await self.interrupt()
        elif event_type == "speech_ended":
            self.session.state = VoiceState.LISTENING

    async def run_response(self, user_text: str) -> None:
        """Stream one assistant response."""
        generation = str(uuid.uuid4())
        self.session.generation_id = generation
        self.session.state = VoiceState.THINKING
        self.session.messages.append(ChatMessage("user", user_text))
        start = time.perf_counter()
        self._response_started[generation] = start

        async def token_stream() -> AsyncIterator[str]:
            async for token in self.llm.generate(self.session.messages):
                await self.emit("assistant.token", generation_id=generation, text=token.text)
                yield token.text

        try:
            self.session.state = VoiceState.SPEAKING
            await self.emit("audio.start", generation_id=generation)

            async for audio in self.tts.synthesize(self.chunker.chunk(token_stream())):
                if generation != self.session.generation_id:
                    return
                if generation not in self._first_audio_seen:
                    self._first_audio_seen.add(generation)
                    TTFB.observe(time.perf_counter() - start)
                AUDIO_CHUNKS.inc()
                await self.emit(
                    "audio.chunk",
                    generation_id=generation,
                    audio=audio.data,
                    sample_rate=audio.sample_rate,
                    channels=audio.channels,
                )
            await self.emit("audio.end", generation_id=generation)
        except asyncio.CancelledError:
            await self.emit("generation.cancelled", generation_id=generation)
            raise
        except Exception as exc:
            ERRORS.inc()
            await self.emit("error", code="PIPELINE_ERROR", message=str(exc), generation_id=generation)
        finally:
            if self.session.generation_id == generation:
                self.session.generation_id = None
                self.session.state = VoiceState.LISTENING

    def start_response(self, text: str) -> None:
        """Start a response task without blocking the caller."""
        if self.session.task and not self.session.task.done():
            self.session.task.cancel()
        self.session.task = asyncio.create_task(self.run_response(text))

    async def close(self) -> None:
        """Cancel work and release provider resources."""
        if self._closed:
            return
        self._closed = True
        self.session.state = VoiceState.DISCONNECTED
        task = self.session.task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.end_utterance()
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
            try:
                await self._stt_task
            except asyncio.CancelledError:
                pass
        await self.tts.cancel()
        await self.stt.close()
        await self.llm.close()
        await self.tts.close()
