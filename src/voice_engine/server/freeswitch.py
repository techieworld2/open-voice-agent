"""Native Windows-compatible FreeSWITCH loopback SIP/RTP gateway."""

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
import heapq
import ipaddress
import logging
import random
import re
import secrets
import socket
import time

from voice_engine.core.audio import AudioProfile, decode_audio
from voice_engine.server.sip import contact_uri, parse_rtp, parse_sdp, parse_sip, rtp_packet, sdp_answer, sip_response, tag


logger = logging.getLogger("voice_engine.freeswitch")
PRESETS = {"clean": (0, 0, 0), "mobile": (80, 20, 1), "unstable": (160, 60, 5)}


class DatagramReceiver:
    def __init__(self, port, callback):
        self.socket = loopback_socket(port)
        self.callback = callback

    async def receive(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                data, address = await loop.sock_recvfrom(self.socket, 65536)
            except OSError as exc:
                if getattr(exc, "winerror", None) in {1234, 10052, 10054}:
                    continue
                raise
            self.callback(data, address)

    def sendto(self, data, address):
        try:
            self.socket.sendto(data, address)
        except OSError as exc:
            if isinstance(exc, BlockingIOError) or getattr(exc, "winerror", None) in {1234, 10052, 10054}:
                return
            raise

    def get_extra_info(self, name):
        return self.socket.getsockname() if name == "sockname" else None

    def close(self):
        self.socket.close()


def loopback_socket(port):
    transport_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        transport_socket.bind(("127.0.0.1", port))
        transport_socket.setblocking(False)
        return transport_socket
    except BaseException:
        transport_socket.close()
        raise


@dataclass
class Dialog:
    request: object
    peer: tuple
    remote_media: tuple
    local_tag: str
    call: object
    response: bytes = b""
    confirmed: bool = False
    held: bool = False
    closing: bool = False
    sequence: int = field(default_factory=lambda: secrets.randbits(16))
    timestamp: int = field(default_factory=lambda: secrets.randbits(32))
    ssrc: int = field(default_factory=lambda: secrets.randbits(32))
    peer_ssrc: int | None = None
    receive_sequence: int | None = None
    receive_start: float = 0
    last_received: float = field(default_factory=time.monotonic)
    incoming: list = field(default_factory=list)
    received: dict = field(default_factory=dict)
    outgoing: list = field(default_factory=list)
    audio: deque = field(default_factory=deque)
    generation: str | None = None
    watermark: tuple | None = None
    clock_task: object = None
    retry_task: object = None
    pipeline: object = None
    packets_received: int = 0
    packets_sent: int = 0
    packets_dropped: int = 0
    concealed_frames: int = 0
    missing_streak: int = 0
    started: float = field(default_factory=time.monotonic)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class FreeSwitchGateway:
    def __init__(self, settings, calls, call_factory, pipeline_factory, acquire_slot, release_slot):
        self.settings, self.calls, self.call_factory = settings, calls, call_factory
        self.pipeline_factory = pipeline_factory
        self.acquire_slot, self.release_slot = acquire_slot, release_slot
        self.sip_transport = self.rtp_transport = None
        self.dialogs: dict[str, Dialog] = {}
        self.pending: set[str] = set()
        self.tasks: set = set()
        self.preset = "clean"
        self.rng = random.Random()
        self.running = False
        self.detail = "FreeSWITCH gateway is disabled"
        self.fs_status = {"connected": False, "detail": "FreeSWITCH has not been checked"}
        self.sip_port = settings.freeswitch_sip_port
        self.rtp_port = settings.freeswitch_rtp_port

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def completed(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception():
                error = done.exception()
                logger.error("FreeSWITCH gateway task failed", exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(completed)
        return task

    async def start(self):
        if len(self.settings.call_lab_token) < 24:
            self.detail = "Generate the FreeSWITCH lab configuration before enabling its gateway"
            return
        try:
            decode_audio(b"\xff", "mulaw")
            self.sip_transport = DatagramReceiver(self.sip_port, self.sip_received)
            self.rtp_transport = DatagramReceiver(self.rtp_port, self.rtp_received)
            self.spawn(self.sip_transport.receive())
            self.spawn(self.rtp_transport.receive())
            self.sip_port = self.sip_transport.get_extra_info("sockname")[1]
            self.rtp_port = self.rtp_transport.get_extra_info("sockname")[1]
            self.running = True
            self.detail = "Loopback SIP/RTP gateway is listening"
            self.spawn(self._probe_loop())
        except (OSError, ValueError) as exc:
            if self.sip_transport:
                self.sip_transport.close()
            if self.rtp_transport:
                self.rtp_transport.close()
            self.detail = f"Cannot start the FreeSWITCH gateway: {exc}"
            logger.error(self.detail)

    def snapshot(self):
        return {"backend": "freeswitch", "running": self.running, "detail": self.detail,
                "sip_port": self.sip_port, "rtp_port": self.rtp_port, "freeswitch": self.fs_status,
                "playback_basis": "RTP transmission estimate with a conservative playout margin; handset playback is not measured"}

    def network_snapshot(self):
        delay, jitter, loss = PRESETS[self.preset]
        return {"available": self.running, "preset": self.preset, "scope": "freeswitch_engine_rtp_leg",
                "directions": ["inbound", "outbound"], "delay_ms": delay, "jitter_ms": jitter,
                "loss_percent": loss, "detail": "FreeSWITCH-to-engine RTP leg; phone-to-FreeSWITCH traffic is unchanged"}

    def set_network(self, preset):
        if not isinstance(preset, str) or preset not in PRESETS:
            raise ValueError("Select clean, mobile, or unstable")
        self.preset = preset
        for dialog in self.dialogs.values():
            dialog.packets_dropped += len(dialog.incoming) + len(dialog.received)
            dialog.incoming.clear()
            dialog.received.clear()
            dialog.receive_sequence = None
            dialog.missing_streak = 0
            dialog.call.record("network.changed", **self.network_snapshot())
        return self.network_snapshot()

    def impairment(self):
        delay, jitter, loss = PRESETS[self.preset]
        dropped = self.rng.random() * 100 < loss
        return max(0, delay + self.rng.uniform(-jitter, jitter)) / 1000, dropped

    def sip_received(self, data, peer):
        if not self.running or not ipaddress.ip_address(peer[0]).is_loopback:
            return
        try:
            message = parse_sip(data)
        except (ValueError, UnicodeError):
            return
        if message.start.startswith("SIP/2.0"):
            return
        method, identity = message.method, message.headers["call-id"]
        if message.headers["cseq"].split()[1] != method:
            self.reply(message, peer, 400, "Bad Request")
            return
        dialog = self.dialogs.get(identity)
        if method == "OPTIONS":
            self.reply(message, peer, 200, "OK")
        elif method == "INVITE":
            token = message.headers.get("x-voice-engine-token", "")
            if not dialog and not secrets.compare_digest(token.encode(), self.settings.call_lab_token.encode()):
                self.reply(message, peer, 403, "Forbidden")
                return
            if dialog and dialog.closing:
                self.reply(message, peer, 481, "Call Does Not Exist")
            elif dialog:
                if dialog.peer != peer or tag(message.headers["from"]) != tag(dialog.request.headers["from"]):
                    self.reply(message, peer, 481, "Call Does Not Exist")
                elif message.headers["cseq"] == dialog.request.headers["cseq"]:
                    self.sip_transport.sendto(dialog.response, peer)
                elif tag(message.headers["to"]) != dialog.local_tag:
                    self.reply(message, peer, 481, "Call Does Not Exist")
                else:
                    self.spawn(self.reinvite(dialog, message))
            elif identity in self.pending:
                self.reply(message, peer, 100, "Trying")
            elif len(self.dialogs) + len(self.pending) >= 20:
                self.reply(message, peer, 503, "Service Unavailable")
            else:
                self.pending.add(identity)
                self.spawn(self.invite(message, peer))
        elif method == "CANCEL" and dialog and peer == dialog.peer and message.headers["cseq"].split()[0] == dialog.request.headers["cseq"].split()[0] and tag(message.headers["from"]) == tag(dialog.request.headers["from"]):
            self.reply(message, peer, 200, "OK", dialog.local_tag)
        elif dialog and peer == dialog.peer and tag(message.headers["to"]) == dialog.local_tag and tag(message.headers["from"]) == tag(dialog.request.headers["from"]):
            if method == "ACK":
                if message.headers["cseq"].split()[0] != dialog.request.headers["cseq"].split()[0]:
                    return
                dialog.confirmed = True
                if dialog.retry_task:
                    dialog.retry_task.cancel()
                if not dialog.clock_task:
                    dialog.clock_task = self.spawn(self.clock(dialog))
            elif method == "BYE":
                self.reply(message, peer, 200, "OK", dialog.local_tag)
                self.spawn(self.close_dialog(dialog))
            elif method == "CANCEL":
                self.reply(message, peer, 200, "OK", dialog.local_tag)
            elif method == "INFO":
                self.reply(message, peer, 200, "OK", dialog.local_tag)
                dialog.call.record("gateway.info", detail="SIP INFO received")
            else:
                self.reply(message, peer, 501, "Not Implemented", dialog.local_tag)
        elif method != "ACK":
            self.reply(message, peer, 481, "Call Does Not Exist")

    def reply(self, message, peer, code, reason, local_tag="", body=""):
        response = sip_response(message, code, reason, local_tag, body, self.sip_port)
        self.sip_transport.sendto(response, peer)
        return response

    async def invite(self, message, peer):
        identity = message.headers["call-id"]
        acquired = False
        dialog = None
        try:
            if not tag(message.headers["from"]) or message.headers.get("content-type", "").split(";", 1)[0].lower() != "application/sdp":
                raise ValueError("Expected an SDP offer and a From tag")
            remote, held = parse_sdp(message.body)
            contact_uri(message.headers.get("contact", ""))
            if held or any(item.remote_media == remote for item in self.dialogs.values()):
                raise ValueError("The initial media endpoint must be active and unique")
            acquired = await self.acquire_slot()
            if not acquired:
                self.reply(message, peer, 503, "Service Unavailable")
                return
            for old_id in list(self.calls):
                if len(self.calls) < 20:
                    break
                if self.calls[old_id].ended_at:
                    del self.calls[old_id]
            if len(self.calls) >= 20:
                self.reply(message, peer, 503, "Service Unavailable")
                return
            call = self.call_factory(channel="FreeSWITCH / " + identity[:100])
            dialog = Dialog(message, peer, remote, secrets.token_hex(10), call)
            dialog.pipeline = self.pipeline_factory(AudioProfile.select("telephony", "mulaw"), lambda event: self.sink(dialog, event))
            dialog.pipeline.session.session_id = call.id
            call.pipeline, call.bridge = dialog.pipeline, dialog
            call.record("call.started", codec="PCMU", sample_rate=8000, backend="freeswitch")
            self.dialogs[identity], self.calls[call.id] = dialog, call
            dialog.response = self.reply(message, peer, 200, "OK", dialog.local_tag,
                                         sdp_answer(self.rtp_port, int(time.time())))
            dialog.retry_task = self.spawn(self.retry_answer(dialog))
        except (ValueError, TypeError):
            self.reply(message, peer, 488, "Not Acceptable Here")
        except Exception:
            logger.exception("Cannot create FreeSWITCH voice pipeline")
            self.reply(message, peer, 500, "Server Internal Error")
        finally:
            self.pending.discard(identity)
            if acquired and dialog is None:
                await self.release_slot()
            elif dialog is not None and identity not in self.dialogs:
                try:
                    if dialog.pipeline:
                        await dialog.pipeline.close()
                finally:
                    await self.release_slot()

    async def reinvite(self, dialog, message):
        try:
            remote, held = parse_sdp(message.body)
            contact_uri(message.headers.get("contact", ""))
            if int(message.headers["cseq"].split()[0]) <= int(dialog.request.headers["cseq"].split()[0]):
                raise ValueError("Out-of-order re-INVITE")
            if not held and any(item is not dialog and item.remote_media == remote for item in self.dialogs.values()):
                raise ValueError("Media endpoint already in use")
            if not held:
                dialog.remote_media = remote
            dialog.held, dialog.request = held, message
            dialog.last_received = time.monotonic()
            dialog.incoming.clear()
            dialog.received.clear()
            dialog.receive_sequence = None
            dialog.peer_ssrc = None
            dialog.response = self.reply(message, dialog.peer, 200, "OK", dialog.local_tag,
                                         sdp_answer(self.rtp_port, int(time.time()), held))
            dialog.call.record("call.held" if held else "call.resumed")
            if held and dialog.pipeline.playback_active:
                await dialog.pipeline.interrupt()
        except (ValueError, TypeError):
            self.reply(message, dialog.peer, 488, "Not Acceptable Here", dialog.local_tag)

    async def retry_answer(self, dialog):
        interval = .5
        for _ in range(10):
            await asyncio.sleep(interval)
            if dialog.confirmed or dialog.closing:
                return
            self.sip_transport.sendto(dialog.response, dialog.peer)
            interval = min(interval * 2, 4)
        dialog.call.record("error", code="SIP_ACK_TIMEOUT", message="FreeSWITCH did not acknowledge the call answer")
        await self.close_dialog(dialog)

    def rtp_received(self, data, peer):
        dialog = next((item for item in self.dialogs.values() if not item.held and item.remote_media == peer), None)
        parsed = parse_rtp(data)
        if not dialog or dialog.closing or parsed is None:
            return
        sequence, timestamp, ssrc, payload = parsed
        if dialog.peer_ssrc is not None and dialog.peer_ssrc != ssrc:
            return
        dialog.peer_ssrc = ssrc
        now = time.monotonic()
        dialog.last_received = now
        dialog.packets_received += 1
        delay, dropped = self.impairment()
        if dialog.receive_sequence is not None:
            distance = (sequence - dialog.receive_sequence) & 65535
            if (distance >= 32768 and dialog.missing_streak >= 3) or 128 <= distance < 32768:
                dialog.receive_sequence = None
                dialog.incoming.clear()
                dialog.received.clear()
                dialog.missing_streak = 0
                dialog.call.record("rtp.resync", reason="Receive clock drift or sustained late packets")
        if dialog.receive_sequence is None:
            dialog.receive_sequence = sequence
            base, jitter, _ = PRESETS[self.preset]
            dialog.receive_start = now + (base + jitter + 60) / 1000
        if dropped or len(dialog.incoming) >= 200:
            dialog.packets_dropped += 1
            return
        heapq.heappush(dialog.incoming, (now + delay, sequence, payload))

    async def sink(self, dialog, event):
        data = dict(event.data)
        if event.type == "audio.start":
            dialog.generation = str(data["generation_id"])
            dialog.watermark = None
        elif event.type == "audio.chunk":
            if dialog.closing or dialog.held or data["generation_id"] != dialog.generation:
                return
            audio = data.pop("audio")
            if len(audio) != 160 or len(dialog.audio) >= 100:
                raise ValueError("FreeSWITCH RTP output queue overflow or invalid audio frame")
            dialog.audio.append((dialog.generation, int(data["end_sample"]), audio))
            dialog.call.outbound_frames += 1
            return
        elif event.type == "barge_in_clear":
            started = time.perf_counter()
            dialog.generation = None
            dialog.audio.clear()
            dialog.outgoing.clear()
            dialog.watermark = None
            dialog.call.record("metrics", gateway_queue_clear_ms=(time.perf_counter() - started) * 1000)
        elif event.type == "input.audio":
            dialog.call.publish_input(data)
            return
        dialog.call.record(event.type, **data)

    async def clock(self, dialog):
        next_frame = time.monotonic()
        try:
            while not dialog.closing:
                now = time.monotonic()
                if not dialog.held and now - dialog.last_received > 30:
                    dialog.call.record("error", code="RTP_TIMEOUT", message="No RTP received from FreeSWITCH for 30 seconds")
                    break
                while dialog.incoming and dialog.incoming[0][0] <= now:
                    _, sequence, payload = heapq.heappop(dialog.incoming)
                    if dialog.receive_sequence is not None and ((sequence - dialog.receive_sequence) & 65535) < 128:
                        dialog.received[sequence] = payload
                if not dialog.held and dialog.receive_sequence is not None and now >= dialog.receive_start:
                    payload = dialog.received.pop(dialog.receive_sequence, None)
                    if payload is None:
                        dialog.concealed_frames += 1
                        dialog.missing_streak += 1
                    else:
                        dialog.missing_streak = 0
                    dialog.receive_sequence = (dialog.receive_sequence + 1) & 65535
                    await dialog.pipeline.process_audio(payload if payload is not None else bytes([255]) * 160)
                    dialog.call.inbound_frames += 1
                if not dialog.held:
                    generation, sample, payload = dialog.audio.popleft() if dialog.audio else (None, 0, bytes([255]) * 160)
                    packet = rtp_packet(payload, dialog.sequence, dialog.timestamp, dialog.ssrc)
                    dialog.sequence = (dialog.sequence + 1) & 65535
                    dialog.timestamp = (dialog.timestamp + 160) & 0xffffffff
                    delay, dropped = self.impairment()
                    if not dropped and len(dialog.outgoing) < 200:
                        heapq.heappush(dialog.outgoing, (now + delay, dialog.packets_sent + dialog.sequence, packet, generation, sample))
                    else:
                        dialog.packets_dropped += 1
                while dialog.outgoing and dialog.outgoing[0][0] <= now:
                    _, _, packet, generation, sample = heapq.heappop(dialog.outgoing)
                    if generation is not None and generation != dialog.generation:
                        continue
                    self.rtp_transport.sendto(packet, dialog.remote_media)
                    dialog.packets_sent += 1
                    if generation is not None:
                        previous = dialog.watermark
                        if previous is None or sample > previous[1]:
                            dialog.watermark = generation, sample, now
                if dialog.watermark:
                    generation, sample, sent_at = dialog.watermark
                    margin = max(0, self.settings.freeswitch_playout_margin_ms / 1000 - (now - sent_at))
                    await dialog.pipeline.acknowledge_playback(generation, max(0, sample - int(margin * 8000)))
                if dialog.packets_sent % 50 == 0:
                    dialog.call.record("rtp.stats", received=dialog.packets_received, sent=dialog.packets_sent,
                                       simulated_drops=dialog.packets_dropped, concealed_frames=dialog.concealed_frames)
                next_frame = max(next_frame + .02, time.monotonic())
                await asyncio.sleep(max(0, next_frame - time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("FreeSWITCH RTP processing failed")
            dialog.call.record("error", code="RTP_ERROR", message="RTP processing failed; inspect server logs")
        finally:
            if not dialog.closing:
                await self.close_dialog(dialog, notify=True)

    async def hangup(self, dialog):
        await self.close_dialog(dialog, notify=True)

    async def close_dialog(self, dialog, notify=False):
        if dialog.closing:
            return
        dialog.closing = True
        if notify and self.sip_transport:
            request = dialog.request
            packet = (f"BYE {contact_uri(request.headers['contact'])} SIP/2.0\r\n"
                      f"Via: SIP/2.0/UDP 127.0.0.1:{self.sip_port};branch=z9hG4bK{secrets.token_hex(8)}\r\n"
                      f"From: {request.headers['to'].split(';tag=')[0]};tag={dialog.local_tag}\r\n"
                      f"To: {request.headers['from']}\r\nCall-ID: {request.headers['call-id']}\r\n"
                      "CSeq: 1 BYE\r\nMax-Forwards: 70\r\nContent-Length: 0\r\n\r\n").encode()
            self.sip_transport.sendto(packet, dialog.peer)
        for task in (dialog.clock_task, dialog.retry_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        try:
            await dialog.pipeline.close()
        finally:
            dialog.call.ended_at = time.time()
            dialog.call.record("call.ended")
            dialog.call.pipeline = dialog.call.bridge = None
            self.dialogs.pop(dialog.request.headers["call-id"], None)
            await self.release_slot()

    async def _esl_message(self, reader):
        headers = {}
        for _ in range(32):
            line = await asyncio.wait_for(reader.readline(), 1)
            if not line or len(line) > 8192:
                raise ValueError("Invalid FreeSWITCH event socket response")
            if not line.strip():
                break
            key, _, value = line.decode().partition(":")
            headers[key.lower()] = value.strip()
        size = int(headers.get("content-length", "0"))
        if not 0 <= size <= 32768:
            raise ValueError("FreeSWITCH response exceeds its limit")
        body = await asyncio.wait_for(reader.readexactly(size), 1) if size else b""
        return headers, body.decode(errors="replace")

    async def probe(self):
        writer = None
        if not self.settings.freeswitch_esl_password:
            return {"connected": False, "detail": "Generate the FreeSWITCH lab configuration first"}
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.settings.freeswitch_esl_port), 1)
            headers, _ = await self._esl_message(reader)
            if headers.get("content-type") != "auth/request":
                raise ValueError("Expected FreeSWITCH event socket authentication")
            writer.write(("auth " + self.settings.freeswitch_esl_password + "\n\n").encode())
            await writer.drain()
            headers, _ = await self._esl_message(reader)
            if not headers.get("reply-text", "").startswith("+OK"):
                raise ValueError("FreeSWITCH event socket authentication failed")
            writer.write(b"api version\n\n")
            await writer.drain()
            _, version = await self._esl_message(reader)
            writer.write(b"api sofia status\n\n")
            await writer.drain()
            _, profiles = await self._esl_message(reader)
            profiles_ready = all(re.search(r"(?m)^\s*" + name + r"\s+[^\n]*\bRUNNING\b", profiles)
                                 for name in ("voice-lab", "engine-loopback"))
            return {"connected": True, "version": version.strip()[:160], "profiles_ready": profiles_ready,
                    "detail": "FreeSWITCH profiles are running" if profiles_ready else "Start FreeSWITCH with the generated lab configuration"}
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            return {"connected": False, "detail": "FreeSWITCH event socket is unavailable; start its generated lab configuration"}
        finally:
            if writer:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

    async def _probe_loop(self):
        while self.running:
            self.fs_status = await self.probe()
            await asyncio.sleep(5)

    async def close(self):
        self.running = False
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for dialog in list(self.dialogs.values()):
            await self.close_dialog(dialog, notify=True)
        for transport in (self.sip_transport, self.rtp_transport):
            if transport:
                transport.close()
