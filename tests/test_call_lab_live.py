import os
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv("VOICE_ENGINE_RUN_SIP_TESTS") != "1", reason="Requires the running native FreeSWITCH lab; opt in with VOICE_ENGINE_RUN_SIP_TESTS=1")
def test_live_sip_registration_rtp_audio_and_barge_in():
    from tools.sip_probe import run_probe

    path = Path(__file__).resolve().parents[1] / "call-lab" / "freeswitch" / "lab.env"
    values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))
    assert all(values.get("VOICE_ENGINE_" + kind, "mock") == "mock" for kind in ("STT", "LLM", "TTS"))
    result = run_probe(values["CALL_LAB_HOST"], values["CALL_LAB_SIP_PASSWORD"], values["VOICE_ENGINE_AUTH_TOKEN"],
                       port=int(values.get("CALL_LAB_SIP_PORT", "5060")))
    assert result["sip_registered"] and result["call_answered"]
    assert result["voiced_rtp_packets"] > 0
    assert result["metrics"]["barge_in_cancel_latency_ms"] >= 0
