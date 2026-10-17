"""FastAPI reference transport."""

import asyncio
import json
import logging
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from voice_engine.config import Settings
from voice_engine.core.agent import VoiceAgentPipeline
from voice_engine.core.audio import AudioProfile, decode_audio
from voice_engine.core.types import PipelineEvent
from voice_engine.observability.logging import configure_logging
from voice_engine.observability.metrics import ACTIVE_CONNECTIONS, SESSIONS
from voice_engine.providers.registry import create
from voice_engine.server.call_lab import create_call_lab_router
from voice_engine.vad.energy import EnergyVAD

configure_logging()
logger = logging.getLogger("voice_engine.server")
settings = Settings()


def _load_optional_provider(kind: str, name: str) -> None:
    """Import a provider module so its @register decorator runs."""
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
STATIC = BASE / "static"
if not STATIC.is_dir():
    STATIC = Path(sys.prefix) / "share" / "open-voice-engine" / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
_benchmark_lock = asyncio.Lock()


@app.get("/")
async def index() -> FileResponse:
    """Serve the reference browser client."""
    return FileResponse(STATIC / "index.html")


@app.get("/call-lab")
async def call_lab_page() -> FileResponse:
    return FileResponse(STATIC / "call-lab.html")


def _create_pipeline(profile, sink):
    telephony = profile.mode == "telephony"
    return VoiceAgentPipeline(
        stt=create("stt", settings.stt), llm=create("llm", settings.llm), tts=create("tts", settings.tts),
        vad=EnergyVAD(
            sample_rate=profile.sample_rate,
            speech_threshold=settings.telephony_speech_threshold if telephony else settings.vad_speech_threshold,
            silence_threshold=settings.telephony_silence_threshold if telephony else settings.vad_silence_threshold,
            min_speech_ms=settings.vad_min_speech_ms, min_silence_ms=profile.base_silence_ms,
        ), event_sink=sink, profile=profile,
    )


async def _acquire_lab_slot():
    global _connection_count
    async with _connection_lock:
        if _connection_count >= settings.max_connections:
            return False
        _connection_count += 1
        ACTIVE_CONNECTIONS.inc()
        SESSIONS.inc()
        return True


async def _release_lab_slot():
    global _connection_count
    async with _connection_lock:
        _connection_count -= 1
        ACTIVE_CONNECTIONS.dec()


call_lab_router = create_call_lab_router(settings, _create_pipeline, _acquire_lab_slot, _release_lab_slot)
app.include_router(call_lab_router)


@app.get("/api/benchmark")
async def benchmark(request: Request, concurrency: int = Query(30, ge=1, le=100),
                    mode: str = "telephony") -> dict:
    """Bounded loopback-only benchmark; honors the same token and origin policy."""
    if settings.auth_token:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(token, settings.auth_token):
            raise HTTPException(401, "Invalid benchmark token")
    origin = request.headers.get("origin")
    if origin and not _origin_allowed(origin):
        raise HTTPException(403, "Origin not allowed")
    try:
        AudioProfile.select(mode)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if concurrency > settings.max_connections - _connection_count:
        raise HTTPException(409, "Not enough connection slots for this benchmark")
    if _benchmark_lock.locked():
        raise HTTPException(409, "A benchmark is already running")
    from voice_engine.benchmarks.stress_tester import run_benchmark

    async with _benchmark_lock:
        port = request.scope.get("server", (None, settings.port))[1] or settings.port
        result = await run_benchmark(concurrency, mode, f"ws://127.0.0.1:{port}/ws/voice",
                                     token=settings.auth_token,
                                     origin=next(iter(settings.allowed_origins), "http://localhost:8000"))
        result["providers"] = {"stt": settings.stt, "llm": settings.llm, "tts": settings.tts}
        return result


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


def _vad_config(pipeline: VoiceAgentPipeline) -> dict:
    """Report current thresholds/durations, e.g. to prefill a client's tuning UI."""
    vad = pipeline.vad
    return {
        "speech_threshold": vad.speech_threshold,
        "silence_threshold": vad.silence_threshold,
        "min_speech_ms": vad.min_speech_frames * vad.frame_ms,
        "min_silence_ms": pipeline.detector.base_silence_ms,
        "effective_silence_ms": vad.min_silence_frames * vad.frame_ms,
        "trailing_silence_ms": pipeline.detector.trailing_silence_ms,
        "semantic_eot": pipeline.detector.semantic_enabled,
        "barge_in_min_speech_ms": pipeline.guard.min_speech_ms,
        "filter_backchannels": pipeline.guard.filter_backchannels,
        "revision": pipeline.vad_revision,
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

    mode = websocket.query_params.get("mode", settings.mode)
    codec = websocket.query_params.get("codec", settings.telephony_codec if mode == "telephony" else "pcm_s16le")
    try:
        profile = AudioProfile.select(mode, codec)

        if codec == "mulaw":
            decode_audio(b"\xff", codec)
    except ValueError:
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
        if event.type in {"audio.chunk", "input.audio"}:
            audio = payload.pop("audio")
            payload["type"] = "audio" if event.type == "audio.chunk" else "input.audio"
            async with send_lock:
                await websocket.send_json(payload)
                await websocket.send_bytes(audio)
            return
        payload["type"] = event.type
        async with send_lock:
            await websocket.send_json(payload)

    pipeline = None
    try:
        pipeline = _create_pipeline(profile, sink)
        SESSIONS.inc()
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                frame = message["bytes"]
                try:
                    if len(frame) > settings.max_frame_bytes:
                        raise ValueError("Frame exceeds configured byte limit")
                    await pipeline.process_audio(frame)
                except ValueError as exc:
                    await sink(PipelineEvent("error", {"code": "INVALID_AUDIO_FRAME", "message": str(exc)}))
                    if "30 seconds" in str(exc):
                        break
                continue

            raw = message.get("text")
            if raw is None:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await sink(PipelineEvent("error", {"code": "INVALID_JSON"}))
                continue

            if not isinstance(payload, dict):
                await sink(PipelineEvent("error", {"code": "INVALID_JSON", "message": "Expected a JSON object"}))
                continue

            kind = payload.get("type")
            if kind == "session.start":
                pipeline.session.session_id = str(payload.get("session_id", pipeline.session.session_id))
                await sink(PipelineEvent("session.ready", {
                    "type": "session.ready",
                    "session_id": pipeline.session.session_id,
                    "sample_rate": profile.sample_rate,
                    "encoding": profile.encoding,
                    "mode": profile.mode,
                    "frame_ms": profile.frame_ms,
                    "frame_bytes": profile.frame_bytes,
                    "eot_silence_ms": profile.base_silence_ms,
                    "providers": {"stt": settings.stt, "llm": settings.llm, "tts": settings.tts},
                    "response_enabled": pipeline.response_enabled,
                    "models": {"stt": getattr(pipeline.stt, "model_size", None),
                               "llm": getattr(pipeline.llm, "model", None),
                               "tts": getattr(pipeline.tts, "model", None)},
                    "vad": _vad_config(pipeline),
                }))
            elif kind == "session.configure":
                try:
                    await pipeline.configure_response(payload.get("response_enabled"))
                    await pipeline.emit("session.config.ack", response_enabled=pipeline.response_enabled)
                except ValueError as exc:
                    await pipeline.emit("error", code="INVALID_SESSION_CONFIG", message=str(exc))
            elif kind == "input.monitor":
                enabled = payload.get("enabled")
                if isinstance(enabled, bool):
                    pipeline.monitor_input = enabled
                    await pipeline.emit("input.monitor.ack", enabled=enabled, sample_rate=16000)
                else:
                    await pipeline.emit("error", code="INVALID_MONITOR_CONFIG", message="enabled must be a boolean")
            elif kind == "user.text":
                text = str(payload.get("text", "")).strip()
                if text:
                    if pipeline.playback_active:
                        await pipeline.interrupt()
                    pipeline.start_response(text)
            elif kind == "interrupt":
                await pipeline.interrupt()
            elif kind in {"playback.ack", "playback.cleared"}:
                await pipeline.acknowledge_playback(str(payload.get("generation_id", "")),
                                                    payload.get("played_samples", 0),
                                                    cleared=kind == "playback.cleared")
            elif kind == "vad.config":
                try:
                    pipeline.configure_vad(
                        speech_threshold=payload.get("speech_threshold"),
                        silence_threshold=payload.get("silence_threshold"),
                        min_speech_ms=payload.get("min_speech_ms"),
                        min_silence_ms=payload.get("min_silence_ms"),
                        semantic_eot=payload.get("semantic_eot"),
                        trailing_silence_ms=payload.get("trailing_silence_ms"),
                        barge_in_min_speech_ms=payload.get("barge_in_min_speech_ms"),
                        filter_backchannels=payload.get("filter_backchannels"),
                    )
                    await sink(PipelineEvent("vad.config.ack", {
                        "vad": _vad_config(pipeline),
                        "request_id": payload.get("request_id"),
                    }))
                    await pipeline.emit("turn.window", silence_ms=pipeline.detector.silence_ms,
                                        reason=pipeline.detector.reason)
                except (ValueError, TypeError) as exc:
                    await sink(PipelineEvent("error", {"code": "INVALID_VAD_CONFIG", "message": str(exc),
                                                       "request_id": payload.get("request_id")}))
            elif kind == "session.end":
                break
            else:
                await sink(PipelineEvent("error", {
                    "code":"UNKNOWN_MESSAGE_TYPE",
                    "message":f"Unknown type: {kind}"
                }))
    except WebSocketDisconnect:
        logger.info("client disconnected")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("websocket session failed")
    finally:
        try:
            if pipeline:
                await pipeline.close()
        finally:
            async with _connection_lock:
                _connection_count -= 1
                ACTIVE_CONNECTIONS.dec()
