"""Opt-in SIP/RTP smoke call against the running mock-provider call lab."""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import select
import socket
import struct
import time
from urllib.request import Request, urlopen

from voice_engine.core.audio import AudioProfile, decode_audio, speech_frame
from voice_engine.vad.energy import EnergyVAD


def digest_auth(challenge, method, uri, username, password):
    values = {
        match[0]: match[1] or match[2] for match in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', challenge)
    }
    if values.get("algorithm", "MD5").upper() != "MD5":
        raise ValueError("The smoke caller supports the lab's MD5 SIP authentication only")
    realm, nonce = values["realm"], values["nonce"]
    md5 = lambda text: hashlib.md5(text.encode()).hexdigest()
    first = md5(f"{username}:{realm}:{password}")
    last = md5(f"{method}:{uri}")
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"', 'algorithm=MD5']
    if "qop" in values:
        if "auth" not in values["qop"].split(","):
            raise ValueError("Unsupported SIP authentication qop")
        cnonce = secrets.token_hex(8)
        response = md5(f"{first}:{nonce}:00000001:{cnonce}:auth:{last}")
        parts.extend(['qop=auth', 'nc=00000001', f'cnonce="{cnonce}"'])
    else:
        response = md5(f"{first}:{nonce}:{last}")
    if "opaque" in values:
        parts.append(f'opaque="{values["opaque"]}"')
    parts.append(f'response="{response}"')
    return "Digest " + ", ".join(parts)


def parse_sip(data):
    head, _, body = data.decode("utf-8", errors="replace").partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    return lines[0], headers, body


def rtp_payload(packet):
    if len(packet) < 12 or packet[0] >> 6 != 2 or packet[1] & 127 != 0:
        return None
    offset = 12 + (packet[0] & 15) * 4
    if packet[0] & 16:
        if len(packet) < offset + 4:
            return None
        offset += 4 + struct.unpack_from("!H", packet, offset + 2)[0] * 4
    end = len(packet)
    if packet[0] & 32:
        padding = packet[-1]
        if not padding or padding > end - offset:
            return None
        end -= padding
    return packet[offset:end] if offset <= end else None


def run_probe(host, password, dashboard_token, port=5060, api_port=8000):
    ipaddress.IPv4Address(host)
    sip, rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sip.connect((host, port))
    local_ip, local_port = sip.getsockname()
    rtp.bind((local_ip, 0))
    call_id, tag = secrets.token_hex(12), secrets.token_hex(6)
    cseq, to, contact = 0, None, None
    invite_branch = None
    uri = f"sip:7000@{host}:{port}"

    def request(method, target, body="", authorization="", final_to=None, sequence=None, proxy_auth=False):
        nonlocal cseq, invite_branch
        if sequence is None:
            cseq += 1
            sequence = cseq
        branch = invite_branch if method == "ACK" and final_to and contact is None else "z9hG4bK" + secrets.token_hex(8)
        if method == "INVITE":
            invite_branch = branch
        lines = [f"{method} {target} SIP/2.0", f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport",
                 "Max-Forwards: 70", f"From: <sip:1001@{host}>;tag={tag}",
                 "To: " + (final_to or f"<sip:{'1001' if method == 'REGISTER' else '7000'}@{host}>"),
                 f"Call-ID: {call_id}", f"CSeq: {sequence} {method}",
                 f"Contact: <sip:1001@{local_ip}:{local_port}>", "User-Agent: OpenVoiceEngine-LabProbe"]
        if method == "REGISTER":
            lines.append("Expires: 120")
        if authorization:
            lines.append(("Proxy-Authorization: " if proxy_auth else "Authorization: ") + authorization)
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body.encode())}")
        packet = ("\r\n".join(lines) + "\r\n\r\n" + body).encode()
        sip.send(packet)
        if method == "ACK":
            return None
        deadline, resend = time.monotonic() + 15, time.monotonic() + .5
        while time.monotonic() < deadline:
            if not select.select([sip], [], [], .1)[0]:
                if time.monotonic() >= resend:
                    sip.send(packet)
                    resend = time.monotonic() + 1
                continue
            status, headers, response_body = parse_sip(sip.recv(65535))
            if not status.startswith("SIP/2.0 ") or headers.get("cseq") != f"{sequence} {method}":
                continue
            code = int(status.split()[1])
            if code >= 200:
                return code, headers, response_body
        raise TimeoutError(f"No final SIP {method} response; check FreeSWITCH, firewall, and LAN IP")

    def authenticated(method, target, body=""):
        result = request(method, target, body)
        if result[0] in {401, 407}:
            proxy_auth = result[0] == 407
            if method == "INVITE":
                request("ACK", target, final_to=result[1]["to"], sequence=cseq)
            auth = digest_auth(result[1]["proxy-authenticate" if proxy_auth else "www-authenticate"], method, target, "1001", password)
            result = request(method, target, body, auth, proxy_auth=proxy_auth)
        if result[0] != 200:
            raise RuntimeError(f"SIP {method} failed with status {result[0]}")
        return result

    started_at = time.time()
    try:
        authenticated("REGISTER", f"sip:{host}:{port}")
        sdp = (f"v=0\r\no=lab 1 1 IN IP4 {local_ip}\r\ns=Voice Lab Probe\r\nc=IN IP4 {local_ip}\r\nt=0 0\r\n"
               f"m=audio {rtp.getsockname()[1]} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n")
        _, headers, answer = authenticated("INVITE", uri, sdp)
        to = headers["to"]
        contact_match = re.search(r"<([^>]+)>", headers.get("contact", ""))
        contact = contact_match.group(1) if contact_match else uri
        request("ACK", contact, final_to=to, sequence=cseq)
        remote_ip = re.search(r"^c=IN IP4 ([^\r\n]+)", answer, re.M)
        remote_port = re.search(r"^m=audio (\d+) RTP/AVP .*", answer, re.M)
        if not remote_ip or not remote_port:
            raise ValueError("Call answer did not contain IPv4 RTP audio")
        remote = (remote_ip.group(1), int(remote_port.group(1)))
        profile, ssrc = AudioProfile.select("telephony", "mulaw"), secrets.randbits(32)
        start, first_audio, barge_start = time.monotonic(), None, None
        packets, voiced_packets, index = 0, 0, 0
        next_frame = start
        while time.monotonic() - start < 14:
            now = time.monotonic()
            if now >= next_frame:
                voiced = now - start < 1 or (barge_start is not None and barge_start <= now < barge_start + .8)
                payload = speech_frame(profile, index) if voiced else bytes([255]) * 160
                packet = struct.pack("!BBHII", 0x80, 0, index & 65535, (index * 160) & 0xffffffff, ssrc) + payload
                rtp.sendto(packet, remote)
                index += 1
                next_frame = max(next_frame + .02, now)
            for _socket in select.select([rtp], [], [], max(0, min(.01, next_frame - time.monotonic())))[0]:
                packet, source = rtp.recvfrom(65535)
                payload = rtp_payload(packet)
                if not payload:
                    continue
                packets += 1
                if EnergyVAD.rms(decode_audio(payload, "mulaw")) > .01:
                    voiced_packets += 1
                    if first_audio is None:
                        first_audio = time.monotonic()
                        barge_start = first_audio + .5
            if barge_start and now >= barge_start + 2:
                break
        request_api = Request(f"http://{host}:{api_port}/api/call-lab", headers={"Authorization": "Bearer " + dashboard_token})
        with urlopen(request_api, timeout=5) as response:
            calls = json.load(response)["calls"]
        call = next((item for item in calls if item["started_at"] >= started_at - 1), None)
        if first_audio is None:
            raise RuntimeError("SIP answered but no voiced TTS RTP arrived; check provider errors and UDP firewall rules")
        if not call or "barge_in_cancel_latency_ms" not in call["metrics"]:
            raise RuntimeError("No barge-in metric observed; run this synthetic-tone probe with mock STT/LLM/TTS")
        return {"sip_registered": True, "call_answered": True, "rtp_packets_received": packets,
                "voiced_rtp_packets": voiced_packets, "first_tts_audio_after_speech_ms": round((first_audio - start - 1) * 1000, 1),
                "metrics": call["metrics"]}
    finally:
        if to:
            try:
                request("BYE", contact or uri, final_to=to)
            except (TimeoutError, OSError):
                pass
        sip.close()
        rtp.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("call-lab/freeswitch/lab.env"))
    args = parser.parse_args()
    if not args.config.is_file():
        parser.exit(2, "Run tools.setup_freeswitch_lab and start the native FreeSWITCH lab first.\n")
    values = dict(line.split("=", 1) for line in args.config.read_text().splitlines() if "=" in line and not line.startswith("#"))
    try:
        if any(values.get("VOICE_ENGINE_" + kind, "mock") != "mock" for kind in ("STT", "LLM", "TTS")):
            raise ValueError("The tone-based smoke call requires mock providers")
        print(json.dumps(run_probe(values["CALL_LAB_HOST"], values["CALL_LAB_SIP_PASSWORD"],
                                   values["VOICE_ENGINE_AUTH_TOKEN"], port=int(values.get("CALL_LAB_SIP_PORT", "5060"))), indent=2))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(1, f"Call lab test failed: {exc}\n")


if __name__ == "__main__":
    main()
