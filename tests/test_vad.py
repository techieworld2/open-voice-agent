import struct
from voice_engine.vad.energy import EnergyVAD


def frame(amplitude: int, samples: int = 320) -> bytes:
    return struct.pack("<" + "h" * samples, *([amplitude] * samples))


def test_vad_events():
    vad = EnergyVAD(min_speech_ms=40, min_silence_ms=40)
    events=[]
    for _ in range(2):
        events += [e.type for e in vad.process(frame(5000))]
    assert "speech_started" in events
    for _ in range(2):
        events += [e.type for e in vad.process(frame(0))]
    assert "speech_ended" in events
