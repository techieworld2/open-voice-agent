"""Low-dependency streaming RMS VAD."""

from dataclasses import dataclass
import math
import struct


@dataclass(frozen=True)
class VADEvent:
    """VAD event."""

    type: str
    rms: float


class EnergyVAD:
    """Energy VAD with hysteresis and debounce durations."""

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        speech_threshold: float = 0.02,
        silence_threshold: float = 0.015,
        min_speech_ms: int = 100,
        min_silence_ms: int = 300,
    ) -> None:
        if silence_threshold >= speech_threshold:
            raise ValueError("silence_threshold must be below speech_threshold")
        self.frame_ms = frame_ms
        self.min_speech_frames = max(1, math.ceil(min_speech_ms / frame_ms))
        self.min_silence_frames = max(1, math.ceil(min_silence_ms / frame_ms))
        self.speech_threshold = speech_threshold
        self.silence_threshold = silence_threshold
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0

    @staticmethod
    def rms(pcm: bytes) -> float:
        """Calculate normalized RMS from signed 16-bit PCM."""
        if len(pcm) < 2:
            return 0.0
        count = len(pcm) // 2
        samples = struct.unpack("<" + "h" * count, pcm[:count * 2])
        return math.sqrt(sum(s * s for s in samples) / count) / 32768.0

    def process(self, frame: bytes) -> list[VADEvent]:
        """Process one frame."""
        level = self.rms(frame)
        events: list[VADEvent] = []

        if not self._in_speech:
            self._speech_frames = self._speech_frames + 1 if level >= self.speech_threshold else 0
            if self._speech_frames >= self.min_speech_frames:
                self._in_speech = True
                self._silence_frames = 0
                events.append(VADEvent("speech_started", level))
        else:
            events.append(VADEvent("speaking", level))
            self._silence_frames = self._silence_frames + 1 if level <= self.silence_threshold else 0
            if self._silence_frames >= self.min_silence_frames:
                self._in_speech = False
                self._speech_frames = 0
                self._silence_frames = 0
                events.append(VADEvent("speech_ended", level))

        return events

    @property
    def is_speaking(self) -> bool:
        """Current VAD speech state."""
        return self._in_speech

    def configure(
        self,
        speech_threshold: float | None = None,
        silence_threshold: float | None = None,
        min_speech_ms: float | None = None,
        min_silence_ms: float | None = None,
    ) -> None:
        """Update thresholds/durations live, e.g. from a client-tunable UI."""
        new_speech = speech_threshold if speech_threshold is not None else self.speech_threshold
        new_silence = silence_threshold if silence_threshold is not None else self.silence_threshold
        if new_silence >= new_speech:
            raise ValueError("silence_threshold must be below speech_threshold")
        self.speech_threshold = new_speech
        self.silence_threshold = new_silence
        if min_speech_ms is not None:
            self.min_speech_frames = max(1, math.ceil(min_speech_ms / self.frame_ms))
        if min_silence_ms is not None:
            self.min_silence_frames = max(1, math.ceil(min_silence_ms / self.frame_ms))
