"""Generate an isolated native FreeSWITCH lab without modifying its installation."""

import argparse
import ipaddress
from pathlib import Path
import secrets
import xml.etree.ElementTree as ET


def generate(host: str, directory: Path, sip_port=5060, loopback_port=5090, esl_port=8021, rtp_start=16384) -> dict:
    address = ipaddress.IPv4Address(host)
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError("Use this computer's LAN IPv4 address")
    if any(not 1024 <= port <= 65535 for port in (sip_port, loopback_port, esl_port, rtp_start)) or rtp_start + 127 > 65535 or rtp_start % 2:
        raise ValueError("Use ports between 1024 and 65535 and an even RTP start with room for 128 ports")
    if len({sip_port, loopback_port, 5070, 5072}) != 4:
        raise ValueError("SIP and RTP ports must be distinct")
    if any((directory / name).exists() for name in ("lab.env", "conf/freeswitch.xml")):
        raise FileExistsError("FreeSWITCH lab configuration already exists; its credentials were preserved")
    password, token, dashboard, esl = (secrets.token_urlsafe(24) for _ in range(4))
    document = ET.Element("document", {"type": "freeswitch/xml"})
    section = ET.SubElement(document, "section", {"name": "configuration", "description": "Voice lab configuration"})

    def config(name, description):
        return ET.SubElement(section, "configuration", {"name": name, "description": description})

    def params(parent, values):
        for key, value in values.items():
            ET.SubElement(parent, "param", {"name": key, "value": str(value)})

    modules = ET.SubElement(config("modules.conf", "Lab modules"), "modules")
    for name in ("mod_console", "mod_logfile", "mod_commands", "mod_dptools", "mod_dialplan_xml", "mod_sofia", "mod_spandsp", "mod_event_socket"):
        ET.SubElement(modules, "load", {"module": name})
    logging_profile = ET.SubElement(ET.SubElement(config("logfile.conf", "Lab logs"), "profiles"), "profile", {"name": "default"})
    params(ET.SubElement(logging_profile, "settings"), {"rollover": 10485760, "maximum-rotate": 3})
    ET.SubElement(ET.SubElement(logging_profile, "mappings"), "map", {"name": "all", "value": "info,notice,warning,err,crit,alert"})
    params(ET.SubElement(config("switch.conf", "Lab core"), "settings"), {
        "max-sessions": 40, "sessions-per-second": 30, "rtp-start-port": rtp_start, "rtp-end-port": rtp_start + 126,
    })
    params(ET.SubElement(config("event_socket.conf", "Loopback management"), "settings"), {
        "listen-ip": "127.0.0.1", "listen-port": esl_port, "password": esl, "apply-inbound-acl": "loopback",
    })
    lists = ET.SubElement(config("acl.conf", "Private control access"), "network-lists")
    acl = ET.SubElement(lists, "list", {"name": "loopback", "default": "deny"})
    ET.SubElement(acl, "node", {"type": "allow", "cidr": "127.0.0.0/8"})
    profiles = ET.SubElement(config("sofia.conf", "SIP profiles"), "profiles")
    for name, ip, port, authentication in (("voice-lab", str(address), sip_port, "true"),
                                           ("engine-loopback", "127.0.0.1", loopback_port, "false")):
        profile = ET.SubElement(profiles, "profile", {"name": name})
        ET.SubElement(profile, "aliases")
        ET.SubElement(profile, "gateways")
        domains = ET.SubElement(profile, "domains")
        ET.SubElement(domains, "domain", {"name": "all", "alias": "true", "parse": "false"})
        values = {
            "sip-ip": ip, "rtp-ip": ip, "ext-sip-ip": ip, "ext-rtp-ip": ip, "sip-port": port,
            "dialplan": "XML", "context": "voice-lab", "auth-calls": authentication,
            "inbound-codec-prefs": "PCMU@8000h@20i", "outbound-codec-prefs": "PCMU@8000h@20i",
            "inbound-codec-negotiation": "generous", "rtp-timer-name": "soft", "dtmf-type": "rfc2833",
            "sip-trace": "no", "sip-capture": "no", "manage-presence": "false",
            "accept-blind-reg": "false", "log-auth-failures": "false",
        }
        if name == "voice-lab":
            values.update({"force-register-domain": str(address), "force-register-db-domain": str(address)})
        else:
            values.update({"apply-inbound-acl": "loopback", "disable-register": "true"})
        params(ET.SubElement(profile, "settings"), values)
    dialplan = ET.SubElement(document, "section", {"name": "dialplan", "description": "Local lab routing"})
    context = ET.SubElement(dialplan, "context", {"name": "voice-lab"})
    extension = ET.SubElement(context, "extension", {"name": "voice-engine"})
    condition = ET.SubElement(extension, "condition", {"field": "destination_number", "expression": "^7000$"})
    for application, data in (
        ("set", "hangup_after_bridge=true"), ("set", "bypass_media=false"), ("set", "proxy_media=false"),
        ("bridge", "{absolute_codec_string=PCMU@8000h@20i,jitterbuffer_msec=60:200:20,sip_h_X-Voice-Engine-Token=" + token + "}sofia/engine-loopback/7000@127.0.0.1:5070"),
    ):
        ET.SubElement(condition, "action", {"application": application, "data": data})
    directory_section = ET.SubElement(document, "section", {"name": "directory", "description": "Tester account"})
    domain = ET.SubElement(directory_section, "domain", {"name": str(address)})
    groups = ET.SubElement(domain, "groups")
    users = ET.SubElement(ET.SubElement(groups, "group", {"name": "default"}), "users")
    user = ET.SubElement(users, "user", {"id": "1001"})
    params(ET.SubElement(user, "params"), {"password": password})
    variables = ET.SubElement(user, "variables")
    for key, value in {"user_context": "voice-lab", "effective_caller_id_name": "Voice Lab", "effective_caller_id_number": "1001"}.items():
        ET.SubElement(variables, "variable", {"name": key, "value": value})
    for folder in ("conf", "db", "log", "run"):
        (directory / folder).mkdir(parents=True, exist_ok=True)
    ET.indent(document, space="  ")
    xml = directory / "conf" / "freeswitch.xml"
    with xml.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(ET.tostring(document, encoding="unicode"))
    env = directory / "lab.env"
    with env.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(f"""CALL_LAB_HOST={address}
VOICE_ENGINE_CALL_LAB_ENABLED=true
VOICE_ENGINE_CALL_LAB_TOKEN={token}
VOICE_ENGINE_CALL_LAB_SIP_HOST={address}
VOICE_ENGINE_CALL_LAB_SIP_PORT={sip_port}
VOICE_ENGINE_FREESWITCH_ESL_PASSWORD={esl}
VOICE_ENGINE_FREESWITCH_ESL_PORT={esl_port}
VOICE_ENGINE_FREESWITCH_SIP_PORT=5070
VOICE_ENGINE_FREESWITCH_RTP_PORT=5072
VOICE_ENGINE_FREESWITCH_PLAYOUT_MARGIN_MS=200
VOICE_ENGINE_AUTH_TOKEN={dashboard}
VOICE_ENGINE_ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,http://{address}:8000
VOICE_ENGINE_STT=mock
VOICE_ENGINE_LLM=mock
VOICE_ENGINE_TTS=mock
CALL_LAB_SIP_USERNAME=1001
CALL_LAB_SIP_PASSWORD={password}
CALL_LAB_SIP_PORT={sip_port}
""")
    xml.chmod(0o600)
    env.chmod(0o600)
    return {"host": str(address), "username": "1001", "password": password,
            "extension": "7000", "dashboard_token": dashboard, "sip_port": sip_port}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="This Windows computer's LAN IPv4 address")
    parser.add_argument("--sip-port", type=int, default=5060)
    parser.add_argument("--loopback-port", type=int, default=5090)
    parser.add_argument("--esl-port", type=int, default=8021)
    parser.add_argument("--rtp-start", type=int, default=16384)
    args = parser.parse_args()
    try:
        result = generate(args.host, Path(__file__).resolve().parents[1] / "call-lab" / "freeswitch",
                          args.sip_port, args.loopback_port, args.esl_port, args.rtp_start)
    except (ValueError, FileExistsError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(f"\nSIP server: {result['host']}:{result['sip_port']} (UDP)\nUsername: 1001\nPassword: {result['password']}\n"
          f"Dial: 7000\n\nDashboard token: {result['dashboard_token']}\n\n"
          'python -m tools.run_freeswitch_lab --freeswitch-dir "C:\\Program Files\\FreeSWITCH"\n'
          "Dashboard: http://localhost:8000/call-lab\n")


if __name__ == "__main__":
    main()
