import asyncio
from dataclasses import replace
import importlib
import json
import secrets
import socket
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from tools.setup_freeswitch_lab import generate
from voice_engine.config import Settings
from voice_engine.core.agent import VoiceAgentPipeline
from voice_engine.core.audio import AudioProfile, speech_frame, decode_audio, resample_pcm
from voice_engine.providers.mock.llm import MockLLM
from voice_engine.providers.mock.stt import MockSTT
from voice_engine.providers.mock.tts import MockTTS
from voice_engine.server.call_lab import Call, create_call_lab_router
from voice_engine.server.sip import parse_sip, rtp_packet, sdp_answer, sip_response, tag
from voice_engine.vad.energy import EnergyVAD


TOKEN = "test-bridge-token-at-least-24-characters"


@pytest.fixture
def lab():
    app = FastAPI()
    pipelines, slots = [], []
    config = replace(Settings(), call_lab_enabled=True, call_lab_token=TOKEN,
                     freeswitch_sip_port=0, freeswitch_rtp_port=0, freeswitch_esl_password="",
                     auth_token="", stt="mock", llm="mock", tts="mock")

    def factory(profile, sink):
        pipeline = VoiceAgentPipeline(MockSTT(), MockLLM(), MockTTS(),
                                     EnergyVAD(sample_rate=8000), sink, profile)
        pipelines.append(pipeline)
        return pipeline

    async def acquire():
        slots.append(1)
        return True

    async def release():
        slots.pop()

    router = create_call_lab_router(config, factory, acquire, release)
    app.include_router(router)
    with TestClient(app) as client:
        yield client, pipelines, slots, router
    assert not slots


class Phone:
    def __init__(self, gateway):
        self.gateway = gateway
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("127.0.0.1", 0))
        self.media.bind(("127.0.0.1", 0))
        self.sip.settimeout(2)
        self.identity = secrets.token_hex(12)
        self.local_tag = ""
        self.sequence = 0
        self.ssrc = secrets.randbits(32)
        self.send_request("INVITE", sdp_answer(self.media.getsockname()[1], 1))
        answer = parse_sip(self.sip.recv(16384))
        assert answer.start == "SIP/2.0 200 OK"
        self.local_tag = tag(answer.headers["to"])
        self.send_request("ACK")

    def send_request(self, method, body=""):
        port = self.sip.getsockname()[1]
        headers = [f"{method} sip:7000@127.0.0.1:{self.gateway.sip_port} SIP/2.0",
                   f"Via: SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK{method}",
                   "From: <sip:1001@127.0.0.1>;tag=phone", "To: <sip:7000@127.0.0.1>" + (";tag=" + self.local_tag if self.local_tag else ""),
                   "Call-ID: " + self.identity, f"CSeq: {2 if method == 'BYE' else 1} {method}",
                   f"Contact: <sip:1001@127.0.0.1:{port}>", "X-Voice-Engine-Token: " + TOKEN]
        if body:
            headers.append("Content-Type: application/sdp")
        headers.append(f"Content-Length: {len(body.encode())}")
        self.sip.sendto(("\r\n".join(headers) + "\r\n\r\n" + body).encode(), ("127.0.0.1", self.gateway.sip_port))

    def send_audio(self, payload):
        assert len(payload) % 160 == 0
        for offset in range(0, len(payload), 160):
            packet = rtp_packet(payload[offset:offset + 160], self.sequence, self.sequence * 160, self.ssrc)
            self.media.sendto(packet, ("127.0.0.1", self.gateway.rtp_port))
            self.sequence += 1

    def close(self):
        try:
            self.send_request("BYE")
            self.sip.recv(16384)
        finally:
            self.sip.close()
            self.media.close()


@pytest.fixture
def phone(lab):
    peer = Phone(lab[3].gateway)
    try:
        yield peer
    finally:
        peer.close()


def test_generated_credentials_are_private_unique_and_never_overwritten(tmp_path):
    result = generate("192.168.1.45", tmp_path / "lab")
    other = generate("192.168.1.45", tmp_path / "other")
    assert result["password"] != other["password"]
    env = (tmp_path / "lab" / "lab.env").read_text()
    assert all("VOICE_ENGINE_" + kind + "=mock" in env for kind in ("STT", "LLM", "TTS"))
    assert "VOICE_ENGINE_AUTH_TOKEN=" in env
    assert "CALL_LAB_SIP_PASSWORD=" + result["password"] in env
    with pytest.raises(FileExistsError):
        generate("192.168.1.45", tmp_path / "lab")
    assert (tmp_path / "lab" / "lab.env").read_text() == env
    for host in ("127.0.0.1", "0.0.0.0", "224.0.0.1", "bad\nconfig", "::1"):
        with pytest.raises(ValueError):
            generate(host, tmp_path / "invalid")


def test_call_monitor_streams_received_stt_input_and_cleans_up(lab, phone):
    client, pipelines, _, _ = lab
    frame = speech_frame(AudioProfile.select("telephony", "mulaw"))
    call_id = client.get("/api/call-lab").json()["calls"][0]["id"]
    with client.websocket_connect("/ws/call-lab/monitor") as monitor:
        monitor.send_json({"call_id": call_id})
        ready = monitor.receive_json()
        assert ready["sample_rate"] == 16000 and ready["header_bytes"] == 1
        assert pipelines[0].monitor_input
        phone.send_audio(frame * 6)
        received = [monitor.receive_bytes() for _ in range(6)]
        assert received[0][1:] == resample_pcm(decode_audio(frame, "mulaw"), 8000, 16000)
        assert received[-1][0] == 1
        assert all(len(packet) == 641 for packet in received)
    client.get("/api/call-lab")
    assert not pipelines[0].monitor_input
    assert not any(event["type"] == "input.audio" for event in client.get("/api/call-lab").json()["calls"][0]["events"])


def test_telephony_frames_live_vad_and_hangup(lab, phone):
    client, pipelines, slots, _ = lab
    status = client.get("/api/call-lab").json()
    call = status["calls"][0]
    assert len(slots) == 1 and pipelines[0].profile.frame_bytes == 160
    assert status["backend"] == "freeswitch" and TOKEN not in json.dumps(status)
    response = client.post(f"/api/call-lab/calls/{call['id']}/configure", json={
        "min_silence_ms": 480, "min_speech_ms": 160, "response_enabled": False,
    })
    assert response.status_code == 200
    assert response.json()["vad"]["min_silence_ms"] == 480
    assert not pipelines[0].response_enabled
    phone.send_audio(bytes([255]) * 320)
    deadline = time.monotonic() + 2
    while client.get("/api/call-lab").json()["calls"][0]["inbound_frames"] < 2:
        assert time.monotonic() < deadline
    assert client.post(f"/api/call-lab/calls/{call['id']}/configure", json={"min_speech_ms": -1}).status_code == 422
    assert client.post(f"/api/call-lab/calls/{call['id']}/configure", json={"response_enabled": "no"}).status_code == 422
    assert client.post(f"/api/call-lab/calls/{call['id']}/hangup").status_code == 200
    request = parse_sip(phone.sip.recv(16384))
    assert request.method == "BYE"
    phone.sip.sendto(sip_response(request, 200, "OK"), ("127.0.0.1", phone.gateway.sip_port))
    assert not slots
    assert client.get("/api/call-lab").json()["calls"][0]["state"] == "ended"
    assert client.post(f"/api/call-lab/calls/{call['id']}/hangup").status_code == 404


def test_call_monitor_auth_and_origin():
    app = FastAPI()
    config = replace(Settings(), auth_token="private-dashboard-token", allowed_origins=["http://localhost:8000"])
    router = create_call_lab_router(config, None, None, None)
    app.include_router(router)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/ws/call-lab/monitor", headers={"origin": "https://untrusted.example"}):
                pass
        assert denied.value.code == 1008
        with client.websocket_connect("/ws/call-lab/monitor", headers={"origin": "http://localhost:8000"}) as monitor:
            monitor.send_json({"token": "wrong", "call_id": "missing"})
            with pytest.raises(WebSocketDisconnect) as denied:
                monitor.receive_json()
            assert denied.value.code == 1008



def test_slow_monitor_is_bounded_and_call_end_wakes_it():
    call = Call()
    queue = asyncio.Queue(maxsize=50)
    call.monitors.add(queue)
    for index in range(100):
        call.publish_input({"audio": bytes([index]), "vad_speaking": True})
    assert queue.qsize() == 50
    assert queue.get_nowait() == (bytes([50]), True)
    call.record("call.ended")
    remaining = [queue.get_nowait() for _ in range(queue.qsize())]
    assert remaining[-1] is None



def test_dashboard_history_survives_event_ring_overflow():
    call = Call()
    call.record("transcript", text="Hello.", utterance_id=1, final=True)
    call.record("assistant.token", generation_id="g", text="Hello back.")
    for _ in range(250):
        call.record("vad.status", speaking=False)
    assert len(call.events) == 200
    assert call.snapshot()["conversation"] == [
        {"role": "user", "text": "Hello.", "final": True}, {"role": "agent", "text": "Hello back."},
    ]



def test_call_lab_page_and_websocket_console_share_theme_and_navigation(monkeypatch):
    module = importlib.import_module("voice_engine.server.app")
    with TestClient(module.app) as client:
        for path in ("/", "/call-lab"):
            page = client.get(path)
            assert page.status_code == 200
            assert '/static/console.css' in page.text
            assert 'href="/call-lab"' in page.text and 'href="/"' in page.text
        assert client.get("/static/call-lab.js").status_code == 200



def test_lab_api_honors_dashboard_authentication_and_origin():
    async def no_op():
        return True

    app = FastAPI()
    settings = replace(Settings(), call_lab_enabled=False, auth_token="dashboard-secret")
    app.include_router(create_call_lab_router(settings, None, no_op, no_op))
    with TestClient(app) as client:
        assert client.get("/api/call-lab").status_code == 401
        headers = {"Authorization": "Bearer dashboard-secret"}
        assert client.get("/api/call-lab", headers=headers).status_code == 200
        assert client.get("/api/call-lab", headers={**headers, "Origin": "https://untrusted.example"}).status_code == 403
        assert client.get("/api/call-lab/network", headers=headers).status_code == 503
        assert client.post("/api/call-lab/network", headers=headers, json={"preset": "shell"}).status_code == 422
        assert client.post("/api/call-lab/network", headers=headers, json={"preset": []}).status_code == 422
        assert client.post("/api/call-lab/network", headers=headers, content="not json").status_code == 422



def test_sip_probe_digest_and_rtp_parsing():
    import hashlib
    import struct

    from tools.sip_probe import digest_auth, parse_sip, rtp_payload

    auth = digest_auth('Digest realm="voice-lab", nonce="abc", algorithm=MD5',
                       "REGISTER", "sip:lab", "1001", "password")
    md5 = lambda value: hashlib.md5(value.encode()).hexdigest()
    expected = md5(md5("1001:voice-lab:password") + ":abc:" + md5("REGISTER:sip:lab"))
    assert f'response="{expected}"' in auth
    assert 'qop=auth' in digest_auth('Digest realm="voice-lab", nonce="abc", qop="auth"',
                                    "REGISTER", "sip:lab", "1001", "password")
    packet = struct.pack("!BBHII", 0x80, 0, 1, 160, 2) + bytes([255]) * 160
    assert rtp_payload(packet) == bytes([255]) * 160
    assert rtp_payload(packet[:10]) is None
    assert rtp_payload(bytes([0x80, 8]) + packet[2:]) is None
    assert parse_sip(b"SIP/2.0 200 OK\r\nCSeq: 1 REGISTER\r\n\r\n")[1]["cseq"] == "1 REGISTER"
