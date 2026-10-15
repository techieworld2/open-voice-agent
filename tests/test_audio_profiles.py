import struct

import pytest

from voice_engine.core.audio import AudioProfile, decode_audio, encode_audio, resample_pcm
from voice_engine.vad.energy import EnergyVAD


@pytest.mark.parametrize("mode,rate,size", [("wideband", 16000, 640), ("telephony", 8000, 320)])
def test_twenty_ms_profile(mode, rate, size):
    profile = AudioProfile.select(mode)
    assert profile.sample_rate == rate
    assert profile.frame_bytes == size
    assert profile.frame_ms == 20
    pcm = struct.pack("<" + "h" * profile.samples_per_frame, *([6000] * profile.samples_per_frame))
    vad = EnergyVAD(sample_rate=rate, min_speech_ms=320)
    assert all(not vad.process(pcm) for _ in range(15))
    assert vad.process(pcm)[0].type == "speech_started"
    assert len(resample_pcm(pcm, rate, 16000)) == 640


@pytest.mark.parametrize("mode,codec", [("wrong", "pcm_s16le"), ("wideband", "mulaw"), ("telephony", "opus")])
def test_invalid_profiles(mode, codec):
    with pytest.raises(ValueError):
        AudioProfile.select(mode, codec)


def test_mulaw_frames():
    pytest.importorskip("audioop")
    profile = AudioProfile.select("telephony", "mulaw")
    assert profile.frame_bytes == 160
    pcm = struct.pack("<160h", *([6000] * 160))
    encoded = encode_audio(pcm, "mulaw")
    assert len(encoded) == 160
    assert len(decode_audio(encoded, "mulaw")) == 320
    assert abs(EnergyVAD.rms(decode_audio(encoded, "mulaw")) - EnergyVAD.rms(pcm)) < .01


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, "bad", True])
def test_invalid_vad_configuration_is_atomic(value):
    vad = EnergyVAD()
    with pytest.raises(ValueError):
        vad.configure(speech_threshold=.1, min_silence_ms=value)
    assert vad.speech_threshold == .02
