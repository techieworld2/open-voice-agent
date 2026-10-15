"""Real concurrent WebSockets, with no extra third-party client dependencies.

Voiced probes exercise transport/VAD and mock STT, not recognition accuracy.
One completed turn measures TTFA/E2E; a second turn measures interruption.
"""

import argparse
import asyncio
import base64
from contextlib import suppress
import hashlib
import json
import math
import os
import ssl
import struct
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
METRICS = ("ttfa_ms", "end_to_end_turn_latency_ms", "barge_in_cancel_latency_ms")


class WebSocketClient:
    """Minimal RFC6455 client: masking, fragmentation, ping/pong and TLS."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self._send_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, url: str, origin: str) -> "WebSocketClient":
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("Expected ws:// or wss:// URL")
        if "\r" in origin or "\n" in origin:
            raise ValueError("Invalid Origin")
        tls = ssl.create_default_context() if parsed.scheme == "wss" else None
        port = parsed.port or (443 if tls else 80)
        reader, writer = await asyncio.open_connection(parsed.hostname, port, ssl=tls)
        client = cls(reader, writer)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            writer.write((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                          f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                          f"Origin: {origin}\r\n\r\n").encode())
            await writer.drain()
            headers = (await reader.readuntil(b"\r\n\r\n")).decode("latin1")
            lines = headers.split("\r\n")
            if lines[0].split()[1] != "101":
                raise ConnectionError(f"WebSocket handshake rejected: {lines[0]}")
            fields = {key.lower(): value.strip() for key, value in
                      (line.split(":", 1) for line in lines[1:] if ":" in line)}
            expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            if fields.get("sec-websocket-accept", "") != expected:
                raise ConnectionError("Invalid Sec-WebSocket-Accept")
            return client
        except BaseException:
            await client.close()
            raise

    async def send(self, data: bytes | str, opcode: int | None = None) -> None:
        opcode = opcode or (1 if isinstance(data, str) else 2)
        payload = data.encode() if isinstance(data, str) else data
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0xFE]) + struct.pack("!H", size)
        else:
            header += bytes([0xFF]) + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        async with self._send_lock:
            self.writer.write(header + mask + masked)
            await self.writer.drain()

    async def send_json(self, payload: dict) -> None:
        await self.send(json.dumps(payload))

    async def receive(self) -> bytes | str:
        fragments = bytearray()
        message_opcode = None
        while True:
            first, second = await self.reader.readexactly(2)
            opcode, final = first & 15, bool(first & 0x80)
            if first & 0x70 or second & 0x80:
                raise ConnectionError("Invalid server WebSocket frame")
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if size > 1_048_576 or len(fragments) + size > 1_048_576:
                raise ConnectionError("Oversized WebSocket message")
            payload = await self.reader.readexactly(size)
            if opcode == 8:
                raise ConnectionError("Server closed WebSocket")
            if opcode == 9:
                await self.send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode in {1, 2}:
                if message_opcode is not None:
                    raise ConnectionError("Unexpected fragmented message")
                message_opcode = opcode
            elif opcode != 0 or message_opcode is None:
                raise ConnectionError("Invalid WebSocket opcode")
            fragments.extend(payload)
            if final:
                return fragments.decode() if message_opcode == 1 else bytes(fragments)

    async def close(self) -> None:
        if not self.writer.is_closing():
            with suppress(ConnectionError, OSError):
                await self.send(struct.pack("!H", 1000), opcode=8)
            self.writer.close()
            with suppress(ConnectionError, OSError):
                await self.writer.wait_closed()


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(calls: list[dict]) -> dict:
    result = {}
    for metric in METRICS:
        values = [call[metric] for call in calls if metric in call]
        result[metric] = {"p50": percentile(values, 50), "p95": percentile(values, 95),
                          "p99": percentile(values, 99), "max": max(values) if values else None,
                          "samples": len(values)}
    return result


def markdown_table(result: dict) -> str:
    labels = ("Time-to-First-Audio", "End-to-End Turn", "Barge-In Cancellation")
    rows = ["| Metric (ms) | P50 | P95 | P99 | Max | Samples |",
            "|---|---:|---:|---:|---:|---:|"]
    for key, label in zip(METRICS, labels):
        stats = result["metrics"][key]
        values = ["n/a" if stats[name] is None else f"{stats[name]:.2f}" for name in ("p50", "p95", "p99", "max")]
        rows.append(f"| {label} | " + " | ".join(values) + f" | {stats['samples']} |")
    return "\n".join(rows)


async def _call(url: str, mode: str, origin: str, timeout: float, call_id: int) -> dict:
    call = {"call_id": call_id}
    client = None
    sender = None
    try:
        async with asyncio.timeout(timeout):
            client = await WebSocketClient.connect(url, origin)
            await client.send_json({"type": "session.start"})
            ready = json.loads(await client.receive())
            if ready.get("type") != "session.ready":
                raise ConnectionError("Expected session.ready")
            rate = 8000 if mode == "telephony" else 16000
            size = rate // 50
            voiced = struct.pack("<" + "h" * size,
                                 *[int(6500 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(size)])
            silence = bytes(size * 2)
            last_voice = None
            threshold_at = None
            first_generation = None
            completed = False
            interrupting = False
            second_first_at = None
            audio_meta = None

            async def stream_utterance(barge: bool = False) -> None:
                nonlocal last_voice, threshold_at
                clock = time.perf_counter()
                for index in range(20 + 60):
                    await asyncio.sleep(max(0, clock + index * 0.02 - time.perf_counter()))
                    await client.send(voiced if index < 20 else silence)
                    now = time.perf_counter()
                    if index < 20:
                        if barge and index == 15:
                            threshold_at = now
                        if not barge:
                            last_voice = now

            sender = asyncio.create_task(stream_utterance())
            while True:
                message = await client.receive()
                now = time.perf_counter()
                if isinstance(message, bytes):
                    if audio_meta is None or len(message) != size * 2:
                        raise ConnectionError("Invalid outbound audio packet")
                    generation = audio_meta["generation_id"]
                    if first_generation is None:
                        first_generation = generation
                        if last_voice is None:
                            raise RuntimeError("Audio preceded input speech")
                        call["ttfa_ms"] = (now - last_voice) * 1000

                    await client.send_json({"type": "playback.ack", "generation_id": generation,
                                            "played_samples": audio_meta["end_sample"]})
                    if completed and not interrupting:
                        interrupting = True
                        second_first_at = now
                        if sender:
                            await sender
                        await asyncio.sleep(0.08)
                        sender = asyncio.create_task(stream_utterance(True))
                    audio_meta = None
                    continue
                event = json.loads(message)
                kind = event.get("type")
                if kind == "audio":
                    audio_meta = event
                elif kind == "error":
                    raise RuntimeError(f"Server error: {event.get('code')}: {event.get('message', '')}")
                elif kind == "audio.end" and event.get("generation_id") == first_generation and not completed:
                    call["end_to_end_turn_latency_ms"] = (now - last_voice) * 1000
                    completed = True
                    await sender
                    await client.send_json({"type": "user.text", "text": "Please continue for an interruption probe."})
                elif kind == "barge_in_clear" and interrupting:
                    if threshold_at is None:
                        raise RuntimeError("Clear arrived before interruption threshold")
                    call["barge_in_cancel_latency_ms"] = (now - threshold_at) * 1000
                    call["server_cancel_latency_ms"] = event.get("barge_in_cancel_latency_ms")
                    call["second_turn_first_audio_at"] = second_first_at


                    cancelled_generation = event.get("generation_id")
                    until = time.perf_counter() + 0.12
                    while time.perf_counter() < until:
                        try:
                            remaining = await asyncio.wait_for(client.receive(), until - time.perf_counter())
                        except asyncio.TimeoutError:
                            break
                        if isinstance(remaining, str):
                            followup = json.loads(remaining)
                            if followup.get("type") == "audio" and followup.get("generation_id") == cancelled_generation:
                                raise RuntimeError("Stale audio after barge-in clear")
                    call["ok"] = True
                    return call
    except Exception as exc:
        call["ok"] = False
        call["error"] = f"{type(exc).__name__}: {exc}"
        return call
    finally:
        if sender:
            sender.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await sender
        if client:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(client.close(), 1)


async def run_benchmark(concurrency: int = 30, mode: str = "telephony",
                        url: str = "ws://localhost:8000/ws/voice", token: str = "",
                        origin: str = "http://localhost:8000", timeout: float = 30) -> dict:
    if not 1 <= concurrency <= 100:
        raise ValueError("concurrency must be between 1 and 100")
    if mode not in {"telephony", "wideband"}:
        raise ValueError("mode must be telephony or wideband")
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query))
    query.update(mode=mode, codec="pcm_s16le")
    if token:
        query["token"] = token
    address = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    start = time.perf_counter()
    calls = await asyncio.gather(*[_call(address, mode, origin, timeout, i) for i in range(concurrency)])
    successful = [call for call in calls if call.get("ok")]
    return {"concurrency": concurrency, "mode": mode, "successful": len(successful),
            "failed": concurrency - len(successful), "duration_ms": (time.perf_counter() - start) * 1000,
            "metrics": summarize(successful), "calls": calls,
            "measurement": "Client monotonic timestamps; TTFA/E2E from last voiced frame; cancellation from 320ms threshold to clear receipt. Voiced tone probes; mock STT is deterministic."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=30)
    parser.add_argument("--mode", choices=["telephony", "wideband"], default="telephony")
    parser.add_argument("--url", default="ws://localhost:8000/ws/voice")
    parser.add_argument("--origin", default="http://localhost:8000")
    parser.add_argument("--token", default=os.getenv("VOICE_ENGINE_AUTH_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run_benchmark(args.concurrency, args.mode, args.url,
                                      args.token, args.origin, args.timeout))
    print(json.dumps(result, indent=2) if args.json else markdown_table(result))
    if not args.json:
        print(f"\n{result['successful']}/{args.concurrency} calls succeeded ({args.mode}).")
        for call in result["calls"]:
            if not call.get("ok"):
                print(f"Call {call['call_id']}: {call['error']}")
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
