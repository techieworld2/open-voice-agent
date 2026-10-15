# WebSocket protocol

[Back to the README](../README.md) · [Audio tuning guide](audio-guide.md)

## Connect and start

Use `/ws/voice?mode=wideband` or `/ws/voice?mode=telephony&codec=mulaw`. With no query parameters, `.env` supplies the mode and codec. If `VOICE_ENGINE_AUTH_TOKEN` is configured, include `?token=...`. The browser origin must match `VOICE_ENGINE_ALLOWED_ORIGINS`.

```json
{"type":"session.start","session_id":"demo"}
```

`session.ready` reports the session ID, profile, sample rate, codec, frame size, provider names/models, and VAD configuration. The settings include their applied `revision`.

## Audio framing

All input frames are binary, mono, and exactly 20ms long:

| Profile | Encoding | Rate | Frame size |
|---|---|---:|---:|
| Wideband | `pcm_s16le` | 16,000Hz | 640 bytes |
| Telephony | `pcm_s16le` | 8,000Hz | 320 bytes |
| Telephony | `mulaw` | 8,000Hz | 160 bytes |

Invalid modes, codec combinations, and frame sizes are rejected. Input utterances are bounded to 30 seconds; clients must send silence between turns. STT receives 16kHz mono signed little-endian PCM. Outbound TTS is resampled, framed, and paced at the negotiated wire rate.

Each outbound agent `audio` JSON event is immediately followed by its binary frame under a shared transport lock:

```json
{"type":"audio","generation_id":"...","encoding":"pcm_s16le","sample_rate":16000,"start_sample":0,"end_sample":320,"valid_samples":320}
```

Playback state and acknowledgements use the generation's source-sample timeline. `valid_samples` identifies non-padding samples in a final frame.

## Client controls

| Type | Fields / effect |
|---|---|
| `session.start` | Optional `session_id`; returns `session.ready` |
| `session.configure` | Boolean `response_enabled`; disabling cancels active replies and preserves STT |
| `user.text` | `text`; starts a response when responses are enabled |
| `vad.config` | Thresholds, durations, semantic/backchannel switches, optional `request_id` |
| `input.monitor` | Boolean `enabled`; enables server-input audio echo |
| `playback.ack` | `generation_id`, integer `played_samples` |
| `playback.cleared` | Final `generation_id` / `played_samples` at a queue flush |
| `interrupt` | Cancels generation and clears playback |
| `session.end` | Ends the session |

### Live VAD configuration

```json
{
  "type":"vad.config",
  "request_id":12,
  "speech_threshold":0.020,
  "silence_threshold":0.015,
  "min_speech_ms":100,
  "min_silence_ms":300,
  "semantic_eot":true,
  "trailing_silence_ms":950,
  "barge_in_min_speech_ms":320,
  "filter_backchannels":true
}
```

Configuration validates atomically. The server echoes `request_id` in `vad.config.ack` or validation errors; successful acknowledgements include the applied `vad.revision`. Clients may omit `request_id` for backwards compatibility. The browser uses correlation to reject stale acknowledgements and reports older servers that do not support it.

With semantic EoT enabled, the effective silence window switches between configured base/trailing durations. Manual mode uses the base duration. Live changes update pending semantic deadlines as well as subsequent frames.

### Input monitoring

An enabled monitor receives `input.audio` metadata followed by a **640-byte, 20ms, 16kHz PCM** binary frame, independent of the selected wire profile:

```json
{"type":"input.audio","sample_rate":16000,"encoding":"pcm_s16le","vad_speaking":false,"vad_revision":12}
```

These samples match the server conversion supplied to STT. `vad_speaking` describes the exact frame and supports the console's optional gated preview. Monitor packets never acknowledge agent audio and do not depend on an STT/LLM/TTS response.

## Server events

| Type | Meaning |
|---|---|
| `session.ready`, `session.config.ack` | Negotiated session settings and response-mode changes |
| `speech_started`, `speech_ended` | Acoustic utterance boundaries |
| `transcript` | Caller text, `final`, and `utterance_id`; emitted before reply generation |
| `pipeline.stage` | STT capture/processing/completion and LLM/TTS progress |
| `turn.transcribed` | Completed transcription-only turn |
| `turn.window` | Current `silence_ms` and `Complete`, `Trailing Clause`, or `Manual` reason |
| `vad.config.ack` | Applied configuration and its revision |
| `vad.status` | RMS, speech state, silence progress, effective window, and revision; every five frames plus transitions |
| `input.monitor.ack`, `input.audio` | Monitoring state and input PCM metadata |
| `assistant.token` | Generated text for a specific generation |
| `audio.start`, `audio`, `audio.phrase_end`, `audio.end` | Generation-scoped agent audio lifecycle |
| `backchannel.ignored` | Short/passive candidate that leaves agent playback running |
| `barge_in_clear` | Flush playback; includes cancellation latency, spoken/discarded text, and alignment precision |
| `context.sync` | Updated spoken/discarded prefix after the playback watermark is confirmed |
| `generation.cancelled` | Generation stopped |
| `metrics` | TTFA and cancellation/cleanup timing |
| `error` | Validation/provider failure with a code and, when applicable, failing stage |

Legacy `CLEAR_BUFFER` follows `barge_in_clear` for older clients. Error and stage events also appear in terminal logs. Whisper logs model loading and readiness separately; metadata downloads alone do not establish model readiness.

## Playback acknowledgements

```json
{"type":"playback.ack","generation_id":"...","played_samples":4800}
```

Acknowledgements must be generation-scoped, monotonic integers no larger than sent samples. After `barge_in_clear`, the player flushes its queue and sends `playback.cleared` with the final rendered watermark. The pipeline allows up to 250ms for that final acknowledgement before the next LLM request.

Aligned providers confirm completed words through optional chunk-relative `AudioChunk.word_end_samples`. Unaligned providers retain completed phrases conservatively. Physical speaker latency is outside the render watermark guarantee. Without acknowledgements, no spoken assistant prefix is confirmed on interruption.

## Metrics and HTTP endpoints

| Measurement | Start | End |
|---|---|---|
| Console TTFA | Last voiced input frame or text submission | First outbound audio packet; includes EoT/STT |
| Server cancellation | Interruption gate | Generation invalidation |
| Server cleanup | Interruption gate | Task/provider cancellation cleanup |
| Benchmark TTFA | Caller's last voiced input frame | First received binary agent audio |
| Benchmark complete-turn latency | Caller's last voiced frame | Received `audio.end` |
| Benchmark cancellation | 16th voiced interruption frame, 320ms | Received `barge_in_clear` |

Server cancellation excludes browser/transport delay. Benchmark timings use the caller's monotonic clock and include transport, but not physical speaker latency. Prebuffer adds audible delay beyond TTFA.

`/health` provides liveness, `/ready` readiness, and `/metrics` Prometheus output. `GET /api/benchmark?concurrency=30&mode=telephony` runs local callers and returns successful/failed counts, per-call errors, sample counts, and P50/P95/P99/Max metrics. Only one benchmark runs at a time; configured authentication and connection capacity apply. With authentication enabled, use `Authorization: Bearer ...`.
