"""Bounded SIP/SDP and G.711 RTP framing for the loopback FreeSWITCH leg."""

from dataclasses import dataclass
import ipaddress
import re
import struct
from urllib.parse import unquote


@dataclass
class SIPMessage:
    start: str
    headers: dict[str, str]
    body: str

    @property
    def method(self):
        return self.start.split(" ", 1)[0]


def parse_sip(data: bytes) -> SIPMessage:
    if len(data) > 16384 or b"\x00" in data:
        raise ValueError("Invalid SIP packet size")
    text = data.decode("utf-8", errors="strict")
    head, separator, body = text.partition("\r\n\r\n")
    if not separator:
        raise ValueError("Missing SIP header terminator")
    lines = head.split("\r\n")
    if not re.fullmatch(r"(?:[A-Z]+ sip:\S+ SIP/2\.0|SIP/2\.0 \d{3} .+)", lines[0]):
        raise ValueError("Invalid SIP start line")
    headers = {}
    aliases = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length", "c": "content-type"}
    for line in lines[1:]:
        if len(line) > 4096 or ":" not in line or line.startswith((" ", "\t")):
            raise ValueError("Invalid SIP header")
        key, value = line.split(":", 1)
        key = aliases.get(key.lower(), key.lower())
        if key in headers and key != "via":
            raise ValueError("Duplicate SIP header")
        if key == "via" and key in headers:
            headers[key] += "\r\nVia: " + value.strip()
        else:
            headers[key] = value.strip()
    if any(not headers.get(key) for key in ("via", "from", "to", "call-id", "cseq")):
        raise ValueError("Missing SIP dialog headers")
    if len(headers["call-id"]) > 200 or not re.fullmatch(r"\d{1,10} [A-Z]+", headers["cseq"]):
        raise ValueError("Invalid SIP dialog identity")
    if int(headers.get("content-length", "0")) != len(body.encode("utf-8")):
        raise ValueError("SIP Content-Length mismatch")
    return SIPMessage(lines[0], headers, body)


def tag(value: str) -> str:
    match = re.search(r"(?:^|;)tag=([^;\s>]+)", value)
    return match.group(1) if match else ""


def contact_uri(value: str) -> str:
    match = re.search(r"<?(sip:[^>\s]+)>?", value)
    if not match or any(character in unquote(match.group(1)) for character in "\r\n"):
        raise ValueError("Invalid SIP contact")
    return match.group(1)


def parse_sdp(body: str) -> tuple[tuple[str, int], bool]:
    lines = body.splitlines()
    media = [line for line in lines if line.startswith("m=")]
    connection = [line for line in lines if line.startswith("c=IN IP4 ")]
    if len(media) != 1 or not connection:
        raise ValueError("Exactly one IPv4 audio stream is required")
    parts = media[0].split()
    if len(parts) < 4 or parts[0] != "m=audio" or parts[2] != "RTP/AVP" or "0" not in parts[3:]:
        raise ValueError("Only G.711 PCMU RTP is supported")
    port = int(parts[1])
    address = str(ipaddress.IPv4Address(connection[-1][9:].strip()))
    held = address == "0.0.0.0" or port == 0 or any(line in {"a=inactive", "a=recvonly"} for line in lines)
    if not 0 <= port <= 65535 or (not held and not ipaddress.ip_address(address).is_loopback):
        raise ValueError("The FreeSWITCH media leg must stay on loopback")
    if any(line.startswith("a=ptime:") and line != "a=ptime:20" for line in lines):
        raise ValueError("The media leg requires 20ms packets")
    return (address, port), held


def sdp_answer(port: int, session_id: int, held=False) -> str:
    return (f"v=0\r\no=voice {session_id} {session_id} IN IP4 127.0.0.1\r\ns=Open Voice Engine\r\n"
            f"c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {port} RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\na=ptime:20\r\n" + ("a=inactive\r\n" if held else "a=sendrecv\r\n"))


def sip_response(message: SIPMessage, code: int, reason: str, local_tag="", body="", port=5070) -> bytes:
    to = message.headers["to"]
    if local_tag and not tag(to):
        to += ";tag=" + local_tag
    headers = [f"SIP/2.0 {code} {reason}", "Via: " + message.headers["via"],
               "From: " + message.headers["from"], "To: " + to,
               "Call-ID: " + message.headers["call-id"], "CSeq: " + message.headers["cseq"],
               f"Contact: <sip:7000@127.0.0.1:{port}>", "Server: OpenVoiceEngine-FreeSWITCHLab"]
    if body:
        headers.append("Content-Type: application/sdp")
    headers.append(f"Content-Length: {len(body.encode())}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode()


def parse_rtp(packet: bytes):
    if len(packet) < 12 or len(packet) > 2048 or packet[0] >> 6 != 2 or packet[1] & 127 != 0:
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
    if end - offset != 160:
        return None
    sequence, timestamp, ssrc = struct.unpack_from("!HII", packet, 2)
    return sequence, timestamp, ssrc, packet[offset:end]


def rtp_packet(payload: bytes, sequence: int, timestamp: int, ssrc: int) -> bytes:
    if len(payload) != 160:
        raise ValueError("A G.711 packet must contain 160 samples")
    return struct.pack("!BBHII", 0x80, 0, sequence & 65535, timestamp & 0xffffffff, ssrc) + payload
