import asyncio
from dataclasses import replace
import json
import socket

import httpx
import pytest
import pytest_asyncio
import uvicorn

from voice_engine.benchmarks.stress_tester import WebSocketClient, markdown_table, percentile, run_benchmark, summarize
from voice_engine.server import app as server_module


@pytest_asyncio.fixture
async def live_server(monkeypatch):
    monkeypatch.setattr(server_module, "settings", replace(server_module.settings, stt="mock", llm="mock", tts="mock",
                                                        auth_token="", mode="wideband", telephony_codec="pcm_s16le"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(server_module.app, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        yield port
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


def test_percentiles_and_empty_results():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
    assert percentile([], 99) is None
    assert "n/a" in markdown_table({"metrics": summarize([])})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["telephony", "wideband"])
async def test_real_concurrent_websockets_mock_profiles(live_server, mode):
    result = await run_benchmark(3, mode, f"ws://127.0.0.1:{live_server}/ws/voice", timeout=15)
    assert result["successful"] == 3, result["calls"]
    assert result["failed"] == 0
    for stats in result["metrics"].values():
        assert stats["samples"] == 3
        assert 0 <= stats["p50"] <= stats["p95"] <= stats["p99"] <= stats["max"]
    assert "P99" in markdown_table(result)


@pytest.mark.asyncio
async def test_benchmark_api_and_console(live_server):
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{live_server}") as client:
        index = await client.get("/")
        assert "Run 30-Call Concurrency Benchmark" in index.text
        assert (await client.get("/static/audio-worklet.js")).status_code == 200
        response = await client.get("/api/benchmark?concurrency=2&mode=telephony", timeout=15)
        assert response.status_code == 200
        assert response.json()["successful"] == 2, response.text
        assert (await client.get("/api/benchmark?concurrency=0")).status_code == 422
        assert (await client.get("/api/benchmark?mode=wrong")).status_code == 422


@pytest.mark.asyncio
async def test_invalid_frames_and_non_object_json_do_not_crash(live_server):
    client = await WebSocketClient.connect(f"ws://127.0.0.1:{live_server}/ws/voice?mode=telephony", "http://localhost:8000")
    try:
        await client.send_json({"type": "session.start"})
        ready = json.loads(await client.receive())
        assert ready["frame_bytes"] == 320
        await client.send(bytes(640))
        assert json.loads(await client.receive())["code"] == "INVALID_AUDIO_FRAME"
        await client.send("[]")
        assert json.loads(await client.receive())["code"] == "INVALID_JSON"
        await client.send_json({"type": "session.start"})
        assert json.loads(await client.receive())["type"] == "session.ready"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["http://localhost:8000", "http://127.0.0.1:8000"])
async def test_browser_loopback_origins_can_start_sessions(live_server, origin):
    client = await WebSocketClient.connect(f"ws://127.0.0.1:{live_server}/ws/voice?mode=wideband", origin)
    try:
        await client.send_json({"type": "session.start"})
        assert json.loads(await client.receive())["type"] == "session.ready"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unlisted_origin_cannot_start_session(live_server):
    with pytest.raises(ConnectionError, match="403"):
        await WebSocketClient.connect(f"ws://127.0.0.1:{live_server}/ws/voice", "http://unlisted.example:8000")


@pytest.mark.asyncio
async def test_benchmark_requires_configured_auth(live_server, monkeypatch):
    monkeypatch.setattr(server_module, "settings", replace(server_module.settings, auth_token="test-token"))
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{live_server}") as client:
        assert (await client.get("/api/benchmark?concurrency=1")).status_code == 401
        response = await client.get("/api/benchmark?concurrency=1", headers={"Authorization": "Bearer test-token"}, timeout=15)
        assert response.status_code == 200
        assert response.json()["successful"] == 1


@pytest.mark.asyncio
async def test_live_vad_durations_and_semantic_options(live_server):
    client = await WebSocketClient.connect(f"ws://127.0.0.1:{live_server}/ws/voice", "http://localhost:8000")
    try:
        await client.send_json({"type": "session.start"})
        ready = json.loads(await client.receive())
        assert ready["vad"]["semantic_eot"] is True
        await client.send_json({"type": "vad.config", "min_speech_ms": 180, "min_silence_ms": 500,
                                "trailing_silence_ms": 1200, "semantic_eot": False,
                                "barge_in_min_speech_ms": 400, "filter_backchannels": False})
        ack = json.loads(await client.receive())
        assert ack["type"] == "vad.config.ack"
        assert ack["vad"]["min_silence_ms"] == 500
        assert ack["vad"]["min_speech_ms"] == 180
        assert ack["vad"]["barge_in_min_speech_ms"] == 400
        assert ack["vad"]["semantic_eot"] is False
        assert json.loads(await client.receive())["reason"] == "Manual"
        await client.send_json({"type": "vad.config", "semantic_eot": "bad"})
        assert json.loads(await client.receive())["code"] == "INVALID_VAD_CONFIG"
    finally:
        await client.close()
