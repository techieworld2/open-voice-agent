"""Simple mock pipeline latency benchmark."""

import asyncio
import time
from voice_engine.core.agent import VoiceAgentPipeline
from voice_engine.providers.mock.stt import MockSTT
from voice_engine.providers.mock.llm import MockLLM
from voice_engine.providers.mock.tts import MockTTS
from voice_engine.vad.energy import EnergyVAD


async def main() -> None:
    first_audio = None
    started = time.perf_counter()

    async def sink(event):
        nonlocal first_audio
        if event.type == "audio.chunk" and first_audio is None:
            first_audio = time.perf_counter()

    pipeline = VoiceAgentPipeline(MockSTT(),MockLLM(),MockTTS(),EnergyVAD(),sink)
    pipeline.start_response("benchmark")
    await pipeline.session.task
    if first_audio:
        print(f"Mock TTFB: {(first_audio-started)*1000:.1f} ms")
    await pipeline.close()


if __name__ == "__main__":
    asyncio.run(main())
