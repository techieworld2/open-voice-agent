"""Prometheus metrics."""

from prometheus_client import Counter, Gauge, Histogram

ACTIVE_CONNECTIONS = Gauge("voice_engine_active_connections", "Active WebSocket connections")
SESSIONS = Counter("voice_engine_sessions_total", "Total sessions")
INTERRUPTIONS = Counter("voice_engine_interruptions_total", "Total barge-in interruptions")
ERRORS = Counter("voice_engine_errors_total", "Pipeline errors")
TTFB = Histogram(
    "voice_engine_ttfb_seconds",
    "Time from response start to first audio",
    buckets=(.05, .1, .15, .2, .25, .3, .4, .5, .75, 1, 2),
)
AUDIO_CHUNKS = Counter("voice_engine_audio_chunks_total", "Audio chunks emitted")
