"""Gemini TTS adapter (Google GenAI)."""

import asyncio
import os
from typing import AsyncIterator

from voice_engine.core.concurrency import pipeline_ahead
from voice_engine.core.types import AudioChunk
from voice_engine.interfaces.tts import BaseTTS
from voice_engine.providers.registry import register

_SAMPLE_RATE = 24000


async def _non_empty(text: AsyncIterator[str]) -> AsyncIterator[str]:
    async for phrase in text:
        if phrase.strip():
            yield phrase


@register("tts", "gemini")
class GeminiTTS(BaseTTS):
    """Google Gemini TTS provider. One blocking API call per streamed phrase."""

    def __init__(
        self,
        model: str | None = None,
        voice: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model or os.getenv("VOICE_ENGINE_GEMINI_MODEL", "gemini-2.5-flash-preview-tts")
        self.voice = voice or os.getenv("VOICE_ENGINE_GEMINI_VOICE", "Kore")
        self._api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self._cancelled = False
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError(
                    "Install optional dependencies with: pip install -e '.[gemini]'"
                ) from exc
            if not self._api_key:
                raise RuntimeError(
                    "Set GEMINI_API_KEY to use the gemini TTS provider."
                )
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _synthesize_blocking(self, text: str) -> bytes:
        from google.genai import types

        client = self._get_client()
        response = client.models.generate_content(
            model=self.model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                    )
                ),
            ),
        )
        part = response.candidates[0].content.parts[0]
        return part.inline_data.data

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
