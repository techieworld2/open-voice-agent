"""Transparent lexical EoT heuristic layered over streaming energy VAD."""

import re

from voice_engine.core.audio import AudioProfile

TRAILING_WORDS = {"and", "or", "but", "because", "so", "if", "um", "uh", "like", "actually"}
INCOMPLETE_ENDINGS = {"to", "the", "a", "an", "my", "your", "our", "with", "for", "of", "from", "when", "although", "unless"}
PASSIVE_BACKCHANNELS = {"uh-huh", "yeah", "yep", "okay", "right", "mhm"}


def normalize(text: str) -> str:
    return re.sub(r"[^\w\s-]", "", text.lower()).strip()


class TurnDetector:
    def __init__(self, profile: AudioProfile) -> None:
        self.profile = profile
        self.text = ""
        self.semantic_enabled = True
        self.base_silence_ms = profile.base_silence_ms
        self.trailing_silence_ms = profile.trailing_silence_ms

    def update(self, text: str) -> None:
        self.text = text.strip()

    @property
    def trailing(self) -> bool:
        words = normalize(self.text).split()

        return bool(words) and (words[-1] in TRAILING_WORDS | INCOMPLETE_ENDINGS or self.text.endswith((",", ":", ";", "...", "…")))

    @property
    def silence_ms(self) -> int:
        if self.semantic_enabled and self.trailing:
            return max(self.base_silence_ms, self.trailing_silence_ms)
        return self.base_silence_ms

    @property
    def reason(self) -> str:
        if not self.semantic_enabled:
            return "Manual"
        return "Trailing Clause" if self.trailing else "Complete"


class BackchannelGuard:
    """Hold uncertain bursts until speech duration and recognition justify cancellation."""

    min_speech_ms = 320

    def __init__(self) -> None:
        self.filter_backchannels = True

    @staticmethod
    def passive(text: str) -> bool:
        normalized = normalize(text)
        return normalized in PASSIVE_BACKCHANNELS or normalized in {"uh huh", "uhhuh"}

    def confirm(self, voiced_ms: int, text: str, final: bool = False) -> bool:
        if voiced_ms < self.min_speech_ms or (self.filter_backchannels and self.passive(text)):
            return False


        return bool(text.strip())
