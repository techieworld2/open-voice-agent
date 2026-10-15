"""Local faster-whisper STT adapter."""

import asyncio
import logging
import os
import threading
import time
from typing import AsyncIterator

from voice_engine.core.types import TranscriptEvent
from voice_engine.interfaces.stt import BaseSTT
from voice_engine.providers.registry import register

_model_cache: dict[tuple[str, str, str], object] = {}
_model_load_lock = threading.Lock()
logger = logging.getLogger("voice_engine.stt.whisper")


def _load_model(model_size: str, device: str, compute_type: str):
    key = (model_size, device, compute_type)


    with _model_load_lock:
        if key not in _model_cache:
            from faster_whisper import WhisperModel

            started = time.perf_counter()
            logger.info("whisper.model.loading model=%s device=%s compute_type=%s; first use may download weights",
                        model_size, device, compute_type)
            try:
                model = WhisperModel(model_size, device=device, compute_type=compute_type)
            except Exception:
                logger.exception("whisper.model.load_failed model=%s", model_size)
                raise
            _model_cache[key] = model
            logger.info("whisper.model.ready model=%s elapsed_ms=%.1f", model_size,
                        (time.perf_counter() - started) * 1000)
        return _model_cache[key]


@register("stt", "faster_whisper")
class FasterWhisperSTT(BaseSTT):
    """Local Whisper transcription. Buffers one utterance, then transcribes it in one pass."""

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
        language: str | None = None,
        no_speech_threshold: float | None = None,
        logprob_threshold: float | None = None,
    ) -> None:
        self.model_size = model_size or os.getenv("VOICE_ENGINE_WHISPER_MODEL", "base")
        self.device = device or os.getenv("VOICE_ENGINE_WHISPER_DEVICE", "cpu")
        self.compute_type = compute_type or os.getenv("VOICE_ENGINE_WHISPER_COMPUTE_TYPE", "int8")
        self.language = language or os.getenv("VOICE_ENGINE_WHISPER_LANGUAGE") or None
        self.no_speech_threshold = (
            no_speech_threshold
            if no_speech_threshold is not None
            else float(os.getenv("VOICE_ENGINE_WHISPER_NO_SPEECH_THRESHOLD", "0.6"))
        )
        self.logprob_threshold = (
            logprob_threshold
            if logprob_threshold is not None
            else float(os.getenv("VOICE_ENGINE_WHISPER_LOGPROB_THRESHOLD", "-1.0"))
        )

    def _transcribe_blocking(self, pcm: bytes) -> str:
        import numpy as np

        model = _load_model(self.model_size, self.device, self.compute_type)
        started = time.perf_counter()
        logger.info("whisper.transcription.started audio_seconds=%.2f", len(pcm) / 32000)
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        segments, _ = model.transcribe(audio, language=self.language, vad_filter=True)
        kept = []
        for segment in segments:
            if segment.no_speech_prob > self.no_speech_threshold:
                continue
            if segment.avg_logprob < self.logprob_threshold:
                continue
            text = segment.text.strip()
            if text:
                kept.append(text)
        text = " ".join(kept).strip()
        logger.info("whisper.transcription.completed characters=%s elapsed_ms=%.1f",
                    len(text), (time.perf_counter() - started) * 1000)
        return text

    async def transcribe(self, audio: AsyncIterator[bytes]) -> AsyncIterator[TranscriptEvent]:
        buffer = bytearray()
        async for frame in audio:
            buffer.extend(frame)
        if not buffer:
            return
        text = await asyncio.to_thread(self._transcribe_blocking, bytes(buffer))
        if text:
            yield TranscriptEvent(text, True)
