"""FastAPI reference transport."""

import asyncio
import json
import logging
import secrets
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from voice_engine.config import Settings
from voice_engine.core.agent import VoiceAgentPipeline
from voice_engine.core.types import PipelineEvent
from voice_engine.observability.logging import configure_logging
from voice_engine.observability.metrics import ACTIVE_CONNECTIONS, SESSIONS
from voice_engine.providers.mock import stt as _mock_stt
from voice_engine.providers.mock import llm as _mock_llm
from voice_engine.providers.mock import tts as _mock_tts
from voice_engine.providers.registry import create
from voice_engine.vad.energy import EnergyVAD

configure_logging()
logger = logging.getLogger("voice_engine.server")
settings = Settings()


def _load_optional_provider(kind: str, name: str) -> None:
    """Import a provider module so its @register decorator runs."""
    if name == "mock":
        return
    try:
        __import__(f"voice_engine.providers.{name}.{kind}")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"Unknown or unavailable {kind} provider: {name!r}. "
            f"Check VOICE_ENGINE_{kind.upper()} and any optional dependency install."
        ) from exc


_load_optional_provider("tts", settings.tts)
_load_optional_provider("stt", settings.stt)
_load_optional_provider("llm", settings.llm)
app = FastAPI(title="Open Voice Engine", version="0.1.0")
_connection_count = 0
_connection_lock = asyncio.Lock()
BASE = Path(__file__).resolve().parents[3]


@app.get("/")
async def index() -> FileResponse:
    """Serve the reference browser client."""
    return FileResponse(BASE / "static" / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness endpoint."""
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness endpoint."""
    return {"status": "ready"}


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _vad_config(vad: EnergyVAD) -> dict[str, float]:
    """Report current thresholds/durations, e.g. to prefill a client's tuning UI."""
    return {
        "speech_threshold": vad.speech_threshold,
        "silence_threshold": vad.silence_threshold,
        "min_speech_ms": vad.min_speech_frames * vad.frame_ms,
        "min_silence_ms": vad.min_silence_frames * vad.frame_ms,
    }


def _origin_allowed(origin: str | None) -> bool:
    if not settings.allowed_origins or "*" in settings.allowed_origins:
        return True
    return origin in settings.allowed_origins


def _authorized(websocket: WebSocket) -> bool:
    if not settings.auth_token:
        return True
    token = websocket.query_params.get("token")
    return bool(token) and secrets.compare_digest(token, settings.auth_token)


@app.websocket("/ws/voice")
async def voice_socket(websocket: WebSocket) -> None:
    """Handle a bidirectional PCM WebSocket session."""
    global _connection_count

    if not _origin_allowed(websocket.headers.get("origin")) or not _authorized(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    async with _connection_lock:
        if _connection_count >= settings.max_connections:
            await websocket.close(code=1013)
            return
        _connection_count += 1
        ACTIVE_CONNECTIONS.inc()

    send_lock = asyncio.Lock()

    async def sink(event: PipelineEvent) -> None:
        payload = dict(event.data)
        if event.type == "audio.chunk":
            audio = payload.pop("audio")
            payload["type"] = "audio"
            async with send_lock:
                await websocket.send_json(payload)
                await websocket.send_bytes(audio)
            return
        payload["type"] = event.type
        async with send_lock:
            await websocket.send_json(payload)

    pipeline = VoiceAgentPipeline(
        stt=create("stt", settings.stt),
        llm=create("llm", settings.llm),
        tts=create("tts", settings.tts),
        vad=EnergyVAD(
            sample_rate=settings.sample_rate,
            speech_threshold=settings.vad_speech_threshold,
            silence_threshold=settings.vad_silence_threshold,
            min_speech_ms=settings.vad_min_speech_ms,
            min_silence_ms=settings.vad_min_silence_ms,
        ),
        event_sink=sink,
    )
    SESSIONS.inc()

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                frame = message["bytes"]
                if len(frame) == 0 or len(frame) > settings.max_frame_bytes or len(frame) % 2:
                    await websocket.send_json({"type":"error","code":"INVALID_AUDIO_FRAME"})
                    continue
                for event in pipeline.vad.process(frame):
                    if event.type == "speech_started":
                        await pipeline.start_utterance()
                    await pipeline.handle_vad_event(event.type)
                    if event.type == "speech_ended":
                        pipeline.feed_audio(frame)
                        await pipeline.end_utterance()
                if pipeline.vad.is_speaking:
                    pipeline.feed_audio(frame)
                continue

            raw = message.get("text")
            if raw is None:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type":"error","code":"INVALID_JSON"})
                continue

            kind = payload.get("type")
            if kind == "session.start":
                pipeline.session.session_id = str(payload.get("session_id", pipeline.session.session_id))
                await websocket.send_json({
                    "type": "session.ready",
                    "session_id": pipeline.session.session_id,
                    "sample_rate": settings.sample_rate,
                    "encoding": "pcm_s16le",
                    "vad": _vad_config(pipeline.vad),
                })
            elif kind == "user.text":
                text = str(payload.get("text", "")).strip()
                if text:
                    pipeline.start_response(text)
            elif kind == "interrupt":
                await pipeline.interrupt()
            elif kind == "vad.config":
                try:
                    pipeline.vad.configure(
                        speech_threshold=payload.get("speech_threshold"),
                        silence_threshold=payload.get("silence_threshold"),
                        min_speech_ms=payload.get("min_speech_ms"),
                        min_silence_ms=payload.get("min_silence_ms"),
                    )
                    await websocket.send_json({
                        "type": "vad.config.ack",
                        "vad": _vad_config(pipeline.vad),
                    })
                except (ValueError, TypeError) as exc:
                    await websocket.send_json({
                        "type": "error", "code": "INVALID_VAD_CONFIG", "message": str(exc),
                    })
            elif kind == "session.end":
                break
            else:
                await websocket.send_json({
                    "type":"error","code":"UNKNOWN_MESSAGE_TYPE",
                    "message":f"Unknown type: {kind}"
                })
    except WebSocketDisconnect:
        logger.info("client disconnected")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("websocket session failed")
    finally:
        await pipeline.close()
        async with _connection_lock:
            _connection_count -= 1
            ACTIVE_CONNECTIONS.dec()
