"""Shared domain types."""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class VoiceState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class AudioChunk:
    """PCM audio chunk."""

    data: bytes
    sample_rate: int
    channels: int = 1


@dataclass(frozen=True)
class TranscriptEvent:
    """Partial/final transcript."""

    text: str
    final: bool


@dataclass(frozen=True)
class LLMToken:
    """One streamed LLM token."""

    text: str


@dataclass(frozen=True)
class ChatMessage:
    """Conversation message."""

    role: str
    content: str


@dataclass(frozen=True)
class PipelineEvent:
    """Internal pipeline event."""

    type: str
    data: dict[str, Any]
