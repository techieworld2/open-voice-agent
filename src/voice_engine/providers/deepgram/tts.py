"""Deepgram Aura TTS adapter."""

import asyncio
import os
from typing import AsyncIterator

from voice_engine.core.concurrency import pipeline_ahead
from voice_engine.core.types import AudioChunk
from voice_engine.interfaces.tts import BaseTTS
from voice_engine.providers.registry import register

_SAMPLE_RATE = 24000
_URL = "https://api.deepgram.com/v1/speak"


async def _non_empty(text: AsyncIterator[str]) -> AsyncIterator[str]:
    async for phrase in text:
        if phrase.strip():
            yield phrase


@register("tts", "deepgram")
class DeepgramTTS(BaseTTS):
    """Deepgram Aura TTS provider. One blocking REST call per streamed phrase."""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.getenv("VOICE_ENGINE_DEEPGRAM_TTS_MODEL", "aura-2-thalia-en")
        self._api_key = api_key or os.getenv("DEEPGRAM_API_KEY")
        self._cancelled = False

    def _synthesize_blocking(self, text: str) -> bytes:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError(
                "Install optional dependencies with: pip install -e '.[deepgram]'"
            ) from exc
        if not self._api_key:
            raise RuntimeError("Set DEEPGRAM_API_KEY to use the deepgram TTS provider.")

        response = requests.post(
            _URL,
            params={
                "model": self.model,
                "encoding": "linear16",
                "sample_rate": _SAMPLE_RATE,
                "container": "none",
            },
            headers={
                "Authorization": f"Token {self._api_key}",
                "Content-Type": "application/json",
            },
            json={"text": text},
            timeout=30,
        )
        response.raise_for_status()
        return response.content

    async def synthesize(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        self._cancelled = False

        async def synth_one(phrase: str) -> AudioChunk:
            audio = await asyncio.to_thread(self._synthesize_blocking, phrase)
            return AudioChunk(audio, _SAMPLE_RATE, 1)

        async for chunk in pipeline_ahead(_non_empty(text), synth_one, lookahead=1):
            if self._cancelled:
                return
            yield chunk

    async def cancel(self) -> None:
        self._cancelled = True
