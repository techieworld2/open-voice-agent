"""FreeSWITCH SIP call dashboard and received-input monitoring."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import json
import secrets
import time
import uuid

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect


VAD_FIELDS = {
    "speech_threshold", "silence_threshold", "min_speech_ms", "min_silence_ms",
    "semantic_eot", "trailing_silence_ms", "barge_in_min_speech_ms", "filter_backchannels",
}


def vad_snapshot(pipeline) -> dict:
    return {
        "speech_threshold": pipeline.vad.speech_threshold,
        "silence_threshold": pipeline.vad.silence_threshold,
        "min_speech_ms": pipeline.vad.min_speech_frames * pipeline.vad.frame_ms,
        "min_silence_ms": pipeline.detector.base_silence_ms,
        "trailing_silence_ms": pipeline.detector.trailing_silence_ms,
        "semantic_eot": pipeline.detector.semantic_enabled,
        "barge_in_min_speech_ms": pipeline.guard.min_speech_ms,
        "filter_backchannels": pipeline.guard.filter_backchannels,
        "revision": pipeline.vad_revision,
    }


@dataclass
class Call:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    channel: str = ""
    backend: str = "freeswitch"
    playback_basis: str = "RTP transmission estimate; not a handset playback acknowledgement"
    pipeline: object = None
    bridge: object = None
    sequence: int = 0
    events: deque = field(default_factory=lambda: deque(maxlen=200))
    metrics: dict = field(default_factory=dict)
    inbound_frames: int = 0
    outbound_frames: int = 0
    conversation: dict = field(default_factory=dict)
    stages: dict = field(default_factory=dict)
    window: dict = field(default_factory=lambda: {"silence_ms": 350, "reason": "complete"})
    monitors: set = field(default_factory=set, repr=False)

    def publish_input(self, data):
        frame = (bytes(data["audio"]), bool(data.get("vad_speaking")))
        for queue in self.monitors:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    def record(self, kind: str, **data) -> None:
        if kind == "call.ended":
            for queue in self.monitors:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(None)
        self.sequence += 1
        if kind == "metrics":
            self.metrics.update(data)
        elif kind == "pipeline.stage":
            self.stages[str(data.get("stage"))] = data
        elif kind == "turn.window":
            self.window = data
        elif kind in {"transcript", "assistant.token", "context.sync"}:
            key = ("user:" + str(data.get("utterance_id", 0))) if kind == "transcript" else str(data.get("generation_id", ""))
            row = self.conversation.setdefault(key, {"role": "user" if kind == "transcript" else "agent", "text": ""})
            if kind == "transcript":
                row.update(text=str(data.get("text", ""))[:16000], final=data.get("final", False))
            elif kind == "assistant.token":
                row["text"] = (row["text"] + str(data.get("text", "")))[:16000]
            else:
                row.update(text=str(data.get("spoken_text", ""))[:16000],
                           discarded_text=str(data.get("discarded_text", ""))[:16000], interrupted=True)
            while len(self.conversation) > 60:
                del self.conversation[next(iter(self.conversation))]
        self.events.append({"type": kind, "seq": self.sequence, "at": time.time(), **data})

    def snapshot(self) -> dict:
        return {
            "id": self.id, "started_at": self.started_at, "ended_at": self.ended_at,
            "channel": self.channel, "state": "ended" if self.ended_at else "active",
            "events": list(self.events), "metrics": self.metrics,
            "inbound_frames": self.inbound_frames, "outbound_frames": self.outbound_frames,
            "vad": vad_snapshot(self.pipeline) if self.pipeline else {},
            "response_enabled": self.pipeline.response_enabled if self.pipeline else None,
            "conversation": list(self.conversation.values()), "stages": self.stages, "window": self.window,
            "backend": self.backend, "playback_basis": self.playback_basis,
        }


def create_call_lab_router(settings, pipeline_factory, acquire_slot, release_slot):
    from voice_engine.server.freeswitch import FreeSwitchGateway

    calls: dict[str, Call] = {}
    gateway = FreeSwitchGateway(settings, calls, Call, pipeline_factory, acquire_slot, release_slot)

    @asynccontextmanager
    async def lifespan(app):
        if settings.call_lab_enabled:
            await gateway.start()
        try:
            yield
        finally:
            await gateway.close()

    router = APIRouter(lifespan=lifespan)
    router.calls = calls
    router.gateway = gateway

    def authorize(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin and settings.allowed_origins and "*" not in settings.allowed_origins and origin not in settings.allowed_origins:
            raise HTTPException(403, "Origin not allowed")
        if settings.auth_token:
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(token.encode(), settings.auth_token.encode()):
                raise HTTPException(401, "Invalid dashboard token")

    def find_call(call_id: str) -> Call:
        call = calls.get(call_id)
        if not call or call.ended_at:
            raise HTTPException(404, "Active call not found")
        return call

    @router.websocket("/ws/call-lab/monitor")
    async def input_monitor(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin and settings.allowed_origins and "*" not in settings.allowed_origins and origin not in settings.allowed_origins:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        call, queue, tasks = None, None, []
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), 5)
            if len(raw) > 4096:
                raise ValueError("Invalid monitor request")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("token", ""), str):
                raise ValueError("Invalid monitor credentials")
            if settings.auth_token and not secrets.compare_digest(payload.get("token", "").encode(), settings.auth_token.encode()):
                await websocket.close(code=1008)
                return
            call = calls.get(str(payload.get("call_id", "")))
            if not settings.call_lab_enabled or not call or call.ended_at or not call.pipeline:
                raise ValueError("Select an active call to monitor")
            if len(call.monitors) >= 4:
                await websocket.close(code=1013)
                return
            queue = asyncio.Queue(maxsize=50)
            call.monitors.add(queue)
            call.pipeline.monitor_input = True
            await websocket.send_json({"type": "monitor.ready", "sample_rate": 16000, "encoding": "pcm_s16le",
                                       "frame_ms": 20, "header_bytes": 1, "header": "vad_speaking"})

            async def transmit():
                while True:
                    frame = await queue.get()
                    if frame is None:
                        await websocket.close(code=1000)
                        return
                    audio, speaking = frame
                    await asyncio.wait_for(websocket.send_bytes(bytes([speaking]) + audio), 1)

            tasks = [asyncio.create_task(transmit()), asyncio.create_task(websocket.receive_text())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ValueError, TypeError, WebSocketDisconnect, asyncio.TimeoutError):
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1008)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if call and queue is not None:
                call.monitors.discard(queue)
                if call.pipeline:
                    call.pipeline.monitor_input = bool(call.monitors)

    @router.get("/api/call-lab")
    async def status(request: Request):
        authorize(request)
        return {
            "enabled": settings.call_lab_enabled,
            "backend": "freeswitch",
            "configured": len(settings.call_lab_token) >= 24,
            "sip_host": settings.call_lab_sip_host,
            "extension": "7000", "account": "1001", "sip_port": settings.call_lab_sip_port,
            "providers": {"stt": settings.stt, "llm": settings.llm, "tts": settings.tts},
            "playback_precision": gateway.snapshot()["playback_basis"],
            "gateway": gateway.snapshot(),
            "calls": [call.snapshot() for call in reversed(list(calls.values()))],
        }

    @router.post("/api/call-lab/calls/{call_id}/configure")
    async def configure(request: Request, call_id: str, payload: dict):
        authorize(request)
        call = find_call(call_id)
        if not isinstance(payload, dict) or set(payload) - (VAD_FIELDS | {"response_enabled"}):
            raise HTTPException(422, "Unknown call setting")
        try:
            if "response_enabled" in payload and not isinstance(payload["response_enabled"], bool):
                raise ValueError("response_enabled must be a boolean")
            call.pipeline.configure_vad(**{key: value for key, value in payload.items() if key in VAD_FIELDS})
            if "response_enabled" in payload:
                await call.pipeline.configure_response(payload["response_enabled"])
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        call.record("vad.config.ack", vad=vad_snapshot(call.pipeline))
        return {"vad": vad_snapshot(call.pipeline), "response_enabled": call.pipeline.response_enabled}

    @router.post("/api/call-lab/calls/{call_id}/hangup")
    async def hangup(request: Request, call_id: str):
        authorize(request)
        call = find_call(call_id)
        await gateway.hangup(call.bridge)
        return {"status": "requested"}

    @router.get("/api/call-lab/network")
    async def network_status(request: Request):
        authorize(request)
        if not settings.call_lab_enabled or not gateway.running:
            raise HTTPException(503, gateway.detail)
        return gateway.network_snapshot()

    @router.post("/api/call-lab/network")
    async def network_configure(request: Request, payload: dict):
        authorize(request)
        if set(payload) != {"preset"} or not isinstance(payload["preset"], str) or payload["preset"] not in {"clean", "mobile", "unstable"}:
            raise HTTPException(422, "Select clean, mobile, or unstable")
        if not settings.call_lab_enabled or not gateway.running:
            raise HTTPException(503, gateway.detail)
        return gateway.set_network(payload["preset"])


    return router
