"""Runtime configuration."""

from dataclasses import dataclass, field
import os
from pathlib import Path


def load_env(path: Path | None = None) -> None:
    """Load simple KEY=value .env entries without overriding the process environment."""
    path = path or Path.cwd() / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export "), value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


load_env()


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    """Environment-backed settings."""

    host: str = os.getenv("VOICE_ENGINE_HOST", "0.0.0.0")
    port: int = int(os.getenv("VOICE_ENGINE_PORT", "8000"))
    auth_token: str = os.getenv("VOICE_ENGINE_AUTH_TOKEN", "")
    allowed_origins: tuple[str, ...] = _csv(
        "VOICE_ENGINE_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    )
    max_connections: int = int(os.getenv("VOICE_ENGINE_MAX_CONNECTIONS", "100"))
    max_frame_bytes: int = int(os.getenv("VOICE_ENGINE_MAX_FRAME_BYTES", "64000"))
    vad_speech_threshold: float = float(os.getenv("VOICE_ENGINE_VAD_SPEECH_THRESHOLD", "0.020"))
    vad_silence_threshold: float = float(os.getenv("VOICE_ENGINE_VAD_SILENCE_THRESHOLD", "0.015"))
    vad_min_speech_ms: int = int(os.getenv("VOICE_ENGINE_VAD_MIN_SPEECH_MS", "100"))
    stt: str = os.getenv("VOICE_ENGINE_STT", "mock")
    llm: str = os.getenv("VOICE_ENGINE_LLM", "mock")
    tts: str = os.getenv("VOICE_ENGINE_TTS", "mock")
    mode: str = field(default_factory=lambda: os.getenv("VOICE_ENGINE_MODE", "wideband"))
    telephony_codec: str = field(default_factory=lambda: os.getenv("VOICE_ENGINE_TELEPHONY_CODEC", "pcm_s16le"))
    telephony_speech_threshold: float = field(default_factory=lambda: float(os.getenv("VOICE_ENGINE_TELEPHONY_VAD_SPEECH_THRESHOLD", "0.025")))
    telephony_silence_threshold: float = field(default_factory=lambda: float(os.getenv("VOICE_ENGINE_TELEPHONY_VAD_SILENCE_THRESHOLD", "0.018")))
    call_lab_enabled: bool = field(default_factory=lambda: os.getenv("VOICE_ENGINE_CALL_LAB_ENABLED", "false").lower() == "true")
    call_lab_token: str = field(default_factory=lambda: os.getenv("VOICE_ENGINE_CALL_LAB_TOKEN", ""))
    call_lab_sip_host: str = field(default_factory=lambda: os.getenv("VOICE_ENGINE_CALL_LAB_SIP_HOST", ""))
    call_lab_sip_port: int = field(default_factory=lambda: int(os.getenv("VOICE_ENGINE_CALL_LAB_SIP_PORT", "5060")))
    freeswitch_sip_port: int = field(default_factory=lambda: int(os.getenv("VOICE_ENGINE_FREESWITCH_SIP_PORT", "5070")))
    freeswitch_rtp_port: int = field(default_factory=lambda: int(os.getenv("VOICE_ENGINE_FREESWITCH_RTP_PORT", "5072")))
    freeswitch_esl_port: int = field(default_factory=lambda: int(os.getenv("VOICE_ENGINE_FREESWITCH_ESL_PORT", "8021")))
    freeswitch_esl_password: str = field(default_factory=lambda: os.getenv("VOICE_ENGINE_FREESWITCH_ESL_PASSWORD", ""))
    freeswitch_playout_margin_ms: int = field(default_factory=lambda: int(os.getenv("VOICE_ENGINE_FREESWITCH_PLAYOUT_MARGIN_MS", "200")))
