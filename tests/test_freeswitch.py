import asyncio
from dataclasses import replace
import json
import socket
import time
import xml.etree.ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tools.setup_freeswitch_lab import generate
from tools.run_freeswitch_lab import find_freeswitch, read_configuration
from voice_engine.config import Settings
from voice_engine.core.agent import VoiceAgentPipeline
from voice_engine.core.audio import AudioProfile, speech_frame
from voice_engine.providers.mock.llm import MockLLM
from voice_engine.providers.mock.stt import MockSTT
from voice_engine.providers.mock.tts import MockTTS
from voice_engine.server.call_lab import Call, create_call_lab_router
from voice_engine.server.freeswitch import FreeSwitchGateway
from voice_engine.server.sip import parse_rtp, parse_sdp, parse_sip, rtp_packet, sdp_answer, tag
from voice_engine.vad.energy import EnergyVAD


TOKEN = "freeswitch-test-private-token-at-least-24"


class Peer(asyncio.DatagramProtocol):
    def __init__(self):
        self.messages = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def datagram_received(self, data, peer):
        self.messages.put_nowait((data, peer))


def request(method="INVITE", port=5090, rtp_port=20000, token=TOKEN, local_tag="", identity="test-call", cseq=1, body=None):
    if body is None:
        body = sdp_answer(rtp_port, 1) if method == "INVITE" else ""
    lines = [f"{method} sip:7000@127.0.0.1:5070 SIP/2.0",
             f"Via: SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bKtest{cseq}",
             "From: <sip:1001@127.0.0.1>;tag=remote", "To: <sip:7000@127.0.0.1>" + (";tag=" + local_tag if local_tag else ""),
             "Call-ID: " + identity, f"CSeq: {cseq} {method}", f"Contact: <sip:1001@127.0.0.1:{port}>"]
    if token:
        lines.append("X-Voice-Engine-Token: " + token)
    if body:
        lines.append("Content-Type: application/sdp")
    lines.append(f"Content-Length: {len(body.encode())}")
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


@pytest.fixture
async def gateway():
    settings = replace(Settings(), call_lab_enabled=True,
                       call_lab_token=TOKEN, freeswitch_sip_port=0, freeswitch_rtp_port=0,
                       freeswitch_esl_password="", auth_token="", stt="mock", llm="mock", tts="mock")
    slots, calls = [], {}

    def factory(profile, sink):
        return VoiceAgentPipeline(MockSTT(), MockLLM(), MockTTS(), EnergyVAD(sample_rate=8000), sink, profile)

    async def acquire():
        slots.append(1)
        return True

    async def release():
        slots.pop()

    service = FreeSwitchGateway(settings, calls, Call, factory, acquire, release)
    await service.start()
    loop = asyncio.get_running_loop()
    sip_transport, sip_peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    rtp_transport, rtp_peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    try:
        yield service, sip_peer, rtp_peer, slots
    finally:
        await service.close()
        sip_transport.close()
        rtp_transport.close()
        assert not slots


async def invite(service, sip, rtp, **kwargs):
    packet = request(port=sip.port, rtp_port=rtp.port, **kwargs)
    sip.transport.sendto(packet, ("127.0.0.1", service.sip_port))
    data, _ = await asyncio.wait_for(sip.messages.get(), 1)
    return parse_sip(data), packet


async def test_udp_receiver_survives_unreachable_peer(gateway):
    service, sip, _, _ = gateway
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        destination = unavailable.getsockname()
    service.sip_transport.sendto(b"expired peer", destination)
    await asyncio.sleep(.05)
    sip.transport.sendto(request("OPTIONS", port=sip.port), ("127.0.0.1", service.sip_port))
    data, _ = await asyncio.wait_for(sip.messages.get(), 1)
    assert parse_sip(data).start == "SIP/2.0 200 OK"


def test_freeswitch_private_config_and_native_runner(tmp_path):
    result = generate("192.168.1.7", tmp_path)
    config = read_configuration(tmp_path / "lab.env")
    assert config["VOICE_ENGINE_CALL_LAB_ENABLED"] == "true"
    assert config["VOICE_ENGINE_STT"] == config["VOICE_ENGINE_LLM"] == config["VOICE_ENGINE_TTS"] == "mock"
    assert config["CALL_LAB_SIP_PASSWORD"] == result["password"]
    root = ET.parse(tmp_path / "conf/freeswitch.xml").getroot()
    modules = [element.get("module") for element in root.findall(".//load")]
    assert "mod_sofia" in modules and not any("audio_stream" in module for module in modules)
    assert root.find(".//profile[@name='engine-loopback']/settings/param[@name='sip-ip']").get("value") == "127.0.0.1"
    bridge = root.find(".//action[@application='bridge']").get("data")
    assert "sofia/engine-loopback/7000@127.0.0.1:5070" in bridge
    assert config["VOICE_ENGINE_CALL_LAB_TOKEN"] in bridge
    assert root.find(".//user[@id='1001']") is not None
    with pytest.raises(FileExistsError):
        generate("192.168.1.7", tmp_path)
    assert find_freeswitch(tmp_path / "missing") is None
    executable = tmp_path / "freeswitch.exe"
    executable.write_bytes(b"test placeholder")
    assert find_freeswitch(tmp_path) == executable


def test_freeswitch_alternate_ports(tmp_path):
    generate("192.168.1.7", tmp_path, sip_port=5062, loopback_port=5092, esl_port=8022, rtp_start=40000)
    config = read_configuration(tmp_path / "lab.env")
    assert config["VOICE_ENGINE_CALL_LAB_SIP_PORT"] == config["CALL_LAB_SIP_PORT"] == "5062"
    assert config["VOICE_ENGINE_FREESWITCH_ESL_PORT"] == "8022"
    root = ET.parse(tmp_path / "conf/freeswitch.xml").getroot()
    assert root.find(".//profile[@name='voice-lab']/settings/param[@name='sip-port']").get("value") == "5062"
    assert root.find(".//profile[@name='engine-loopback']/settings/param[@name='sip-port']").get("value") == "5092"
    with pytest.raises(ValueError):
        generate("192.168.1.7", tmp_path / "invalid", rtp_start=65500)


def test_sip_and_rtp_boundaries_and_security():
    message = parse_sip(request())
    assert message.method == "INVITE" and message.headers["call-id"] == "test-call"
    assert parse_sdp(message.body) == (("127.0.0.1", 20000), False)
    for data in (b"garbage", request() + b"extra", request().replace(b"Content-Length:", b"Call-ID: other\r\nContent-Length:")):
        with pytest.raises(ValueError):
            parse_sip(data)
    for body in (sdp_answer(20000, 1).replace("127.0.0.1", "8.8.8.8"), sdp_answer(20000, 1).replace("RTP/AVP 0", "RTP/AVP 8"), sdp_answer(20000, 1).replace("ptime:20", "ptime:40")):
        with pytest.raises(ValueError):
            parse_sdp(body)
    packet = rtp_packet(bytes([255]) * 160, 65536, 0x100000000, 10)
    assert parse_rtp(packet) == (0, 0, 10, bytes([255]) * 160)
    assert parse_rtp(packet[:11]) is None
    assert parse_rtp(packet + b"x") is None
    assert parse_rtp(packet[:1] + b"\x08" + packet[2:]) is None


@pytest.mark.asyncio
async def test_actual_udp_sip_negotiation_retransmission_hold_and_hangup(gateway):
    service, sip, rtp, slots = gateway
    response, _ = await invite(service, sip, rtp, token="wrong")
    assert response.start.startswith("SIP/2.0 403") and not slots
    response, packet = await invite(service, sip, rtp)
    assert response.start.startswith("SIP/2.0 200")
    local_tag = tag(response.headers["to"])
    assert local_tag and len(slots) == 1
    sip.transport.sendto(packet, ("127.0.0.1", service.sip_port))
    repeated, _ = await asyncio.wait_for(sip.messages.get(), 1)
    assert tag(parse_sip(repeated).headers["to"]) == local_tag
    assert len(slots) == 1
    sip.transport.sendto(request("ACK", port=sip.port, local_tag=local_tag), ("127.0.0.1", service.sip_port))
    await asyncio.sleep(.03)
    assert service.dialogs["test-call"].confirmed
    held, _ = await invite(service, sip, rtp, local_tag=local_tag, token="", cseq=2, body=sdp_answer(rtp.port, 2, held=True))
    assert held.start.startswith("SIP/2.0 200")
    assert service.dialogs["test-call"].held
    resumed, _ = await invite(service, sip, rtp, local_tag=local_tag, token="", cseq=3)
    assert resumed.start.startswith("SIP/2.0 200")
    assert not service.dialogs["test-call"].held
    sip.transport.sendto(request("BYE", port=sip.port, local_tag=local_tag, cseq=4), ("127.0.0.1", service.sip_port))
    result, _ = await asyncio.wait_for(sip.messages.get(), 1)
    assert parse_sip(result).start.startswith("SIP/2.0 200")
    for _ in range(50):
        if not slots:
            break
        await asyncio.sleep(.01)
    assert not slots
    assert next(iter(service.calls.values())).ended_at is not None


@pytest.mark.asyncio
async def test_real_udp_rtp_mock_turn_barge_in_queue_clear_and_history(gateway):
    service, sip, rtp, _ = gateway
    response, _ = await invite(service, sip, rtp)
    sip.transport.sendto(request("ACK", port=sip.port, local_tag=tag(response.headers["to"])), ("127.0.0.1", service.sip_port))
    dialog = service.dialogs["test-call"]
    profile = AudioProfile.select("telephony", "mulaw")
    index = 0
    next_frame = time.monotonic()

    async def send_frames(count, voiced):
        nonlocal index, next_frame
        for _ in range(count):
            payload = speech_frame(profile, index) if voiced else bytes([255]) * 160
            rtp.transport.sendto(rtp_packet(payload, index, index * 160, 123), ("127.0.0.1", service.rtp_port))
            index += 1
            next_frame += .02
            await asyncio.sleep(max(0, next_frame - time.monotonic()))

    await send_frames(30, True)
    await send_frames(25, False)
    for _ in range(100):
        if dialog.call.outbound_frames >= 25:
            break
        await send_frames(1, False)
    assert dialog.call.outbound_frames >= 25
    assert dialog.call.metrics["ttfa_ms"] > 0
    assert any(row["role"] == "user" and row.get("final") for row in dialog.call.conversation.values())
    received_audio = False
    while not rtp.messages.empty():
        packet, _ = rtp.messages.get_nowait()
        parsed = parse_rtp(packet)
        received_audio |= bool(parsed and parsed[3] != bytes([255]) * 160)
    assert received_audio
    tracker = dialog.pipeline.playback
    assert tracker.played_samples > 0 and dialog.watermark is not None
    await send_frames(22, True)
    for _ in range(30):
        if "barge_in_cancel_latency_ms" in dialog.call.metrics:
            break
        await send_frames(1, True)
    assert "barge_in_cancel_latency_ms" in dialog.call.metrics, (dialog.receive_sequence, index, dialog.pipeline._voiced_ms)
    assert "gateway_queue_clear_ms" in dialog.call.metrics
    assert not dialog.audio and not any(item[3] for item in dialog.outgoing)
    assert tracker.interrupted
    assert dialog.pipeline.session.messages[tracker.message_index].content == tracker.spoken_text
    assert tracker.discarded_text
    assert dialog.call.backend == "freeswitch" and "estimate" in dialog.call.playback_basis


@pytest.mark.asyncio
async def test_network_profiles_operate_on_real_rtp_packets_and_reject_untrusted_sources(gateway):
    service, sip, rtp, _ = gateway
    response, _ = await invite(service, sip, rtp)
    dialog = service.dialogs["test-call"]
    service.set_network("unstable")
    assert service.network_snapshot()["scope"] == "freeswitch_engine_rtp_leg"
    assert service.network_snapshot()["delay_ms"] == 160
    packet = rtp_packet(bytes([255]) * 160, 1, 160, 123)
    service.rtp_received(packet, ("127.0.0.1", rtp.port + 1))
    assert dialog.packets_received == 0
    service.rng.seed(123)
    for index in range(100):
        service.rtp_received(rtp_packet(bytes([255]) * 160, index, index * 160, 123), ("127.0.0.1", rtp.port))
    assert dialog.packets_received == 100
    assert dialog.packets_dropped > 0
    assert len(dialog.incoming) < 100
    assert all(item[0] >= dialog.last_received for item in dialog.incoming)
    service.rtp_received(rtp_packet(bytes([255]) * 160, 101, 16160, 999), ("127.0.0.1", rtp.port))
    assert dialog.packets_received == 100
    with pytest.raises(ValueError):
        service.set_network([])


@pytest.mark.asyncio
async def test_live_network_switch_and_late_packets_reprime_the_receive_buffer(gateway):
    service, sip, rtp, _ = gateway
    await invite(service, sip, rtp)
    dialog = service.dialogs["test-call"]
    peer = ("127.0.0.1", rtp.port)
    service.rtp_received(rtp_packet(bytes([255]) * 160, 100, 16000, 123), peer)
    assert dialog.receive_sequence == 100 and dialog.incoming
    service.set_network("mobile")
    assert dialog.receive_sequence is None and not dialog.incoming
    now = time.monotonic()
    service.rtp_received(rtp_packet(bytes([255]) * 160, 101, 16160, 123), peer)
    assert dialog.receive_sequence == 101 and dialog.receive_start >= now + .15
    dialog.receive_sequence = 120
    dialog.missing_streak = 4
    service.rtp_received(rtp_packet(bytes([255]) * 160, 115, 18400, 123), peer)
    assert dialog.receive_sequence == 115 and dialog.missing_streak == 0
    assert any(event["type"] == "rtp.resync" for event in dialog.call.events)


def test_freeswitch_dashboard_reports_missing_server_without_claiming_ready():
    async def acquire():
        return True

    async def release():
        pass

    app = FastAPI()
    settings = replace(Settings(), call_lab_enabled=True, auth_token="", call_lab_token=TOKEN,
                       freeswitch_sip_port=0, freeswitch_rtp_port=0, freeswitch_esl_password="")
    app.include_router(create_call_lab_router(settings, None, acquire, release))
    with TestClient(app) as client:
        status = client.get("/api/call-lab").json()
        assert status["gateway"]["running"]
        assert not status["gateway"]["freeswitch"]["connected"]
        assert TOKEN not in json.dumps(status)
        response = client.post("/api/call-lab/network", json={"preset": "mobile"})
        assert response.status_code == 200 and response.json()["delay_ms"] == 80
        assert client.post("/api/call-lab/network", json={"preset": []}).status_code == 422


@pytest.mark.asyncio
async def test_freeswitch_pipeline_creation_failure_releases_connection_slot(gateway):
    service, sip, rtp, slots = gateway

    def broken(profile, sink):
        raise RuntimeError("Provider creation failed")

    service.pipeline_factory = broken
    response, _ = await invite(service, sip, rtp)
    assert response.start.startswith("SIP/2.0 500")
    assert not slots and not service.dialogs


@pytest.mark.asyncio
async def test_event_socket_health_authentication_and_profile_detection(gateway):
    service, _, _, _ = gateway
    secret = "private-event-socket-password-24"
    service.settings = replace(service.settings, freeswitch_esl_password=secret)

    async def fake_fs(reader, writer):
        writer.write(b"Content-Type: auth/request\n\n")
        await writer.drain()
        assert (await reader.readuntil(b"\n\n")).decode() == "auth " + secret + "\n\n"
        writer.write(b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n")
        await writer.drain()
        for expected, body in ((b"api version\n\n", b"FreeSWITCH test version"), (b"api sofia status\n\n", b"voice-lab RUNNING\nengine-loopback RUNNING")):
            assert await reader.readuntil(b"\n\n") == expected
            writer.write(f"Content-Type: api/response\nContent-Length: {len(body)}\n\n".encode() + body)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(fake_fs, "127.0.0.1", 0)
    try:
        service.settings = replace(service.settings, freeswitch_esl_port=server.sockets[0].getsockname()[1])
        result = await service.probe()
        assert result["connected"] and result["profiles_ready"]
        assert "test version" in result["version"] and secret not in json.dumps(result)
    finally:
        server.close()
        await server.wait_closed()
