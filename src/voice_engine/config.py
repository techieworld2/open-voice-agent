"""Runtime configuration."""

from dataclasses import dataclass
import os


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    """Environment-backed settings."""

    host: str = os.getenv("VOICE_ENGINE_HOST", "0.0.0.0")
    port: int = int(os.getenv("VOICE_ENGINE_PORT", "8000"))
    auth_token: str = os.getenv("VOICE_ENGINE_AUTH_TOKEN", "")
    allowed_origins: tuple[str, ...] = _csv(
        "VOICE_ENGINE_ALLOWED_ORIGINS", "http://localhost:8000"
    )
    max_connections: int = int(os.getenv("VOICE_ENGINE_MAX_CONNECTIONS", "100"))
    max_frame_bytes: int = int(os.getenv("VOICE_ENGINE_MAX_FRAME_BYTES", "64000"))
    sample_rate: int = int(os.getenv("VOICE_ENGINE_SAMPLE_RATE", "16000"))
    vad_speech_threshold: float = float(os.getenv("VOICE_ENGINE_VAD_SPEECH_THRESHOLD", "0.020"))
    vad_silence_threshold: float = float(os.getenv("VOICE_ENGINE_VAD_SILENCE_THRESHOLD", "0.015"))
    vad_min_speech_ms: int = int(os.getenv("VOICE_ENGINE_VAD_MIN_SPEECH_MS", "100"))
    vad_min_silence_ms: int = int(os.getenv("VOICE_ENGINE_VAD_MIN_SILENCE_MS", "300"))
    stt: str = os.getenv("VOICE_ENGINE_STT", "mock")
    llm: str = os.getenv("VOICE_ENGINE_LLM", "mock")
    tts: str = os.getenv("VOICE_ENGINE_TTS", "mock")
