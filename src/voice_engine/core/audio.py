"""Wire profiles and provider-independent PCM conversion."""

from dataclasses import dataclass
import math
import struct


@dataclass(frozen=True)
class AudioProfile:
    mode: str
    sample_rate: int
    encoding: str = "pcm_s16le"
    frame_ms: int = 20

    @property
    def samples_per_frame(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def frame_bytes(self) -> int:
        return self.samples_per_frame * (1 if self.encoding == "mulaw" else 2)

    @property
    def base_silence_ms(self) -> int:
        return 350 if self.mode == "telephony" else 300

    @property
    def trailing_silence_ms(self) -> int:
        return 1100 if self.mode == "telephony" else 950

    @classmethod
    def select(cls, mode: str = "wideband", encoding: str = "pcm_s16le") -> "AudioProfile":
        if mode not in {"wideband", "telephony"}:
            raise ValueError("mode must be wideband or telephony")
        if encoding not in {"pcm_s16le", "mulaw"} or (mode == "wideband" and encoding != "pcm_s16le"):
            raise ValueError("mulaw is supported only in telephony mode")
        return cls(mode, 8000 if mode == "telephony" else 16000, encoding)


def _audioop():
    try:
        import audioop
    except ImportError as exc:
        raise ValueError("mulaw requires audioop (Python <=3.12) or audioop-lts on Python >=3.13") from exc
    return audioop


def decode_audio(data: bytes, encoding: str) -> bytes:
    return _audioop().ulaw2lin(data, 2) if encoding == "mulaw" else data


def encode_audio(data: bytes, encoding: str) -> bytes:
    return _audioop().lin2ulaw(data, 2) if encoding == "mulaw" else data


def resample_pcm(data: bytes, source_rate: int, target_rate: int, channels: int = 1) -> bytes:
    """Resample a complete PCM segment; low-pass before integer downsampling.

    STT always receives 16kHz mono PCM, independent of the wire profile.
    Provider chunks remain signed 16-bit PCM and retain their native rate.
    """
    if source_rate <= 0 or target_rate <= 0 or channels < 1 or len(data) % (2 * channels):
        raise ValueError("Invalid provider PCM format")
    if source_rate == target_rate and channels == 1:
        return data
    import numpy as np

    samples = np.frombuffer(data, dtype="<i2").reshape(-1, channels).mean(axis=1)
    if not len(samples):
        return b""
    if source_rate > target_rate:

        cutoff = 0.45 * target_rate / source_rate
        axis = np.arange(-16, 17)
        kernel = 2 * cutoff * np.sinc(2 * cutoff * axis) * np.hamming(33)
        kernel /= kernel.sum()
        samples = np.convolve(np.pad(samples, (16, 16), mode="edge"), kernel, mode="valid")
    count = max(1, round(len(samples) * target_rate / source_rate))
    positions = np.arange(count) * source_rate / target_rate
    result = np.interp(positions, np.arange(len(samples)), samples)
    return np.clip(np.rint(result), -32768, 32767).astype("<i2").tobytes()


def speech_frame(profile: AudioProfile, index: int = 0) -> bytes:
    """Deterministic voiced probe, not an intelligible speech/ASR benchmark."""
    samples = [int(6500 * (math.sin(2 * math.pi * 440 * (index * profile.samples_per_frame + i)
                                         / profile.sample_rate)))
               for i in range(profile.samples_per_frame)]
    return encode_audio(struct.pack("<" + "h" * len(samples), *samples), profile.encoding)
