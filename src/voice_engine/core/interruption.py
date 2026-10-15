"""Playback watermarks: never confuse audio sent with audio rendered."""

from dataclasses import dataclass, field
import asyncio


@dataclass
class PlaybackTracker:
    generation_id: str
    message_index: int
    text: str = ""
    sent_samples: int = 0
    played_samples: int = 0
    marks: list[tuple[int, int]] = field(default_factory=list)
    interrupted: bool = False
    precision: str = "phrase"
    clear_ack: asyncio.Event = field(default_factory=asyncio.Event)

    def mark(self, sample: int, word_count: int) -> None:
        if sample <= self.sent_samples and word_count <= len(self.text.split()):
            self.marks.append((sample, word_count))

    def acknowledge(self, samples: int) -> None:
        if isinstance(samples, int) and not isinstance(samples, bool) and 0 <= samples <= self.sent_samples:
            self.played_samples = max(self.played_samples, samples)

    @property
    def spoken_count(self) -> int:
        return max((count for sample, count in self.marks if sample <= self.played_samples), default=0)

    @property
    def spoken_text(self) -> str:
        return " ".join(self.text.split()[:self.spoken_count])

    @property
    def discarded_text(self) -> str:
        return " ".join(self.text.split()[self.spoken_count:])
