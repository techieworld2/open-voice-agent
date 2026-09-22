"""Optional local Kokoro TTS adapter."""

import asyncio
import io
from typing import AsyncIterator
from voice_engine.core.types import AudioChunk
from voice_engine.interfaces.tts import BaseTTS
from voice_engine.providers.registry import register


@register("tts", "kokoro")
class LocalKokoroTTS(BaseTTS):
    """Run local Kokoro inference off the asyncio event loop."""

    def __init__(self, voice: str = "af_heart", speed: float = 1.0) -> None:
        self.voice = voice
        self.speed = speed
        self._cancelled = False

    def _synthesize_blocking(self, text: str) -> tuple[bytes, int]:
        try:
            from kokoro import KPipeline
            import soundfile as sf
        except ImportError as exc:
            raise RuntimeError("Install optional dependencies with: pip install -e '.[kokoro]'") from exc

        import numpy as np
        pipeline = KPipeline(lang_code="a")
        chunks: list[bytes] = []
        rate = 24000
        for _, _, audio in pipeline(text, voice=self.voice, speed=self.speed):
            if self._cancelled:
                break
            arr = np.asarray(audio)
            buf = io.BytesIO()
            sf.write(buf, arr, rate, format="WAV", subtype="PCM_16")
            buf.seek(0)
            with sf.SoundFile(buf) as wav:
                rate = wav.samplerate
                chunks.append(wav.read(dtype="int16").tobytes())
        return b"".join(chunks), rate

    async def synthesize(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        self._cancelled = False
        async for phrase in text:
            if self._cancelled:
                return
            audio, rate = await asyncio.to_thread(self._synthesize_blocking, phrase)
            if self._cancelled:
                return
            yield AudioChunk(audio, rate, 1)

    async def cancel(self) -> None:
        self._cancelled = True
