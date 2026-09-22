# Open Voice Engine

A lightweight, provider-agnostic runtime for building real-time voice applications. It manages WebSocket audio streaming, Voice Activity Detection (VAD), user interruptions (barge-in), and connects STT, LLM, and TTS models into a low-latency pipeline.

## Features

- **Provider-agnostic**: Easily switch between STT (Whisper), LLMs (Gemini, OpenAI-compatible APIs, local vLLM), and TTS (Kokoro, Deepgram, Gemini).
- **Streaming pipeline**: Streams LLM text output into TTS in short phrases to start audio playback faster.
- **Automatic barge-in**: Server-side VAD detects when the user speaks and instantly cancels in-flight audio generation.
- **Live VAD tuning**: Adjust speech and silence detection thresholds on the fly per session.
- **Built-in browser client**: Includes an HTML/JS test client with AudioWorklet playback and mic sensitivity sliders.
- **Offline mock mode**: Built-in mock providers let you run and test the engine locally without any API keys.

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
voice-engine dev
```

Open `http://localhost:8000`.

The default providers are mock providers, so no API key is required.

## Switching providers

The server picks providers via env vars (see `.env.example`):

```text
VOICE_ENGINE_STT=mock
VOICE_ENGINE_LLM=mock
VOICE_ENGINE_TTS=mock
```

Setting `VOICE_ENGINE_TTS` (or `_STT`/`_LLM`) to anything other than `mock` imports
`voice_engine.providers.<name>.<kind>` on startup so the provider's `@register`
decorator runs, then constructs it via the registry. The core engine does not
import optional providers unless they are selected.

## Kokoro

```bash
pip install -e ".[kokoro]"
```

```text
VOICE_ENGINE_TTS=kokoro
```

Or construct `LocalKokoroTTS` directly from Python.

## Gemini TTS and LLM

```bash
pip install -e ".[gemini]"
```

```text
GEMINI_API_KEY=your-key-here

VOICE_ENGINE_TTS=gemini
VOICE_ENGINE_GEMINI_MODEL=gemini-2.5-flash-preview-tts
VOICE_ENGINE_GEMINI_VOICE=Kore

VOICE_ENGINE_LLM=gemini
VOICE_ENGINE_GEMINI_LLM_MODEL=gemini-3.6-flash
VOICE_ENGINE_GEMINI_LLM_MAX_TOKENS=200
VOICE_ENGINE_GEMINI_LLM_TEMPERATURE=0.7
```

`GeminiTTS` calls the Gemini API once per streamed text phrase and returns
24kHz mono 16-bit PCM. `GeminiLLM` streams tokens from Gemini's async chat
API and honors `VOICE_ENGINE_LLM_SYSTEM_PROMPT` (shared with the other LLM
providers). Both share one `GEMINI_API_KEY`. Get a key at
https://aistudio.google.com/apikey. Gemini model names change over time —
if you get a 404 for a model, the API error message names the current
replacement.

## Deepgram Aura TTS

```bash
pip install -e ".[deepgram]"
```

```text
VOICE_ENGINE_TTS=deepgram
DEEPGRAM_API_KEY=your-key-here
VOICE_ENGINE_DEEPGRAM_TTS_MODEL=aura-2-thalia-en
```

`DeepgramTTS` calls the Deepgram `/v1/speak` REST endpoint once per streamed
text phrase, requesting raw `linear16` PCM at 24kHz. Get an API key at
https://console.deepgram.com.

## Local faster-whisper STT

```bash
pip install -e ".[whisper]"
```

```text
VOICE_ENGINE_STT=faster_whisper
VOICE_ENGINE_WHISPER_MODEL=large-v3
VOICE_ENGINE_WHISPER_DEVICE=cpu
VOICE_ENGINE_WHISPER_COMPUTE_TYPE=int8
VOICE_ENGINE_WHISPER_NO_SPEECH_THRESHOLD=0.6
VOICE_ENGINE_WHISPER_LOGPROB_THRESHOLD=-1.0
```

`FasterWhisperSTT` buffers one full utterance (the audio between the VAD's
`speech_started`/`speech_ended` events) and transcribes it in a single
blocking call, run off the event loop via `asyncio.to_thread`. The model is
cached per `(model_size, device, compute_type)` so it only loads once per
process, not per connection.

On CPU, `large-v3` is slow (real-time factor well above 1x on most machines —
expect several seconds of latency per utterance). If your `~/.cache/huggingface`
already has `mobiuslabsgmbh/faster-whisper-large-v3-turbo` downloaded, set
`VOICE_ENGINE_WHISPER_MODEL=large-v3-turbo` for meaningfully faster CPU inference
at a small accuracy cost. Use `VOICE_ENGINE_WHISPER_DEVICE=cuda` only if your
`ctranslate2` build was compiled with CUDA support (`python -c "import ctranslate2;
ctranslate2.get_cuda_device_count()"` — many aarch64 pip wheels are CPU-only).

The energy VAD gates *when* audio reaches this provider, but it can't tell
speech from a loud non-speech noise (a cough, a sneeze, throat-clearing, a
chair scraping behind you) — those cross the same RMS threshold and would
otherwise get "transcribed" into hallucinated text, which then gets treated
as a real user utterance and gets a spoken reply. This provider guards
against that two ways: `vad_filter=True` runs Silero VAD (bundled with
faster-whisper) over the buffered audio first, dropping non-speech stretches
before they reach the decoder at all; and any decoded segment is still
dropped if Whisper's own `no_speech_prob` is above
`VOICE_ENGINE_WHISPER_NO_SPEECH_THRESHOLD` or its `avg_logprob` is below
`VOICE_ENGINE_WHISPER_LOGPROB_THRESHOLD` — the standard low-confidence signals
for a hallucinated segment. If nothing survives both filters, no
`transcript` event is emitted and no response is triggered.

## Generic OpenAI-compatible LLM (vLLM, LM Studio, text-generation-webui, ...)

```bash
pip install -e ".[openai_completions]"
```

```text
VOICE_ENGINE_LLM=openai_completions
VOICE_ENGINE_LLM_URL=http://localhost:5000/v1/completions
VOICE_ENGINE_LLM_MODEL=your-model-id
VOICE_ENGINE_LLM_API_KEY=
VOICE_ENGINE_LLM_SYSTEM_PROMPT=You are a helpful, concise voice assistant. Keep replies short.
VOICE_ENGINE_LLM_MAX_TOKENS=200
VOICE_ENGINE_LLM_TEMPERATURE=0.7
```

`OpenAICompletionsLLM` streams tokens from any server that speaks the legacy
OpenAI `/v1/completions` API (`stream: true`, SSE `data: {...}` lines ending in
`data: [DONE]`) — this is what vLLM, text-generation-webui, LM Studio, and
llama.cpp's server all expose. It builds a plain `System:`/`User:`/`Assistant:`
prompt from the conversation history rather than a model-specific chat
template, so it's a reasonable default across different base/instruct models
but not tuned for any one of them. Point `VOICE_ENGINE_LLM_URL` at your own
inference server; there's no dependency on any specific model or vendor.

## Sample-rate mismatch simulator (reference client)

The reference client (`static/index.html`) has a "Playback sample-rate
simulation" dropdown. TTS audio always carries its real sample rate in the
metadata packet that precedes each binary chunk, and the client normally
resamples it to your speakers' native rate before playback — get this step
wrong (as the very first version of this client did) and you get sped-up,
pitch-shifted "chipmunk" audio instead of a mismatched-rate error, which is
a good bug to understand if you're new to audio engineering. The dropdown lets
you deliberately force the wrong assumed rate so you can hear that failure mode
on demand, then switch back to "Auto" to hear the correct behavior.

## Configuration

Environment variables:

```text
VOICE_ENGINE_HOST=0.0.0.0
VOICE_ENGINE_PORT=8000
VOICE_ENGINE_AUTH_TOKEN=
VOICE_ENGINE_ALLOWED_ORIGINS=http://localhost:8000
VOICE_ENGINE_MAX_CONNECTIONS=100
VOICE_ENGINE_MAX_FRAME_BYTES=64000
VOICE_ENGINE_SAMPLE_RATE=16000
VOICE_ENGINE_VAD_SPEECH_THRESHOLD=0.020
VOICE_ENGINE_VAD_SILENCE_THRESHOLD=0.015
```

## WebSocket protocol

Client starts with:

```json
{"type":"session.start","session_id":"demo","sample_rate":16000,"channels":1,"encoding":"pcm_s16le"}
```

Then it sends binary signed 16-bit little-endian mono PCM.

Barge-in is automatic and server-driven, not a client message: if the VAD
fires `speech_started` while the assistant is `THINKING`/`SPEAKING`, the
pipeline cancels the in-flight generation itself (see `handle_vad_event` in
`core/agent.py`). There's no separate "interrupt" trigger to wire up — just
keep streaming mic audio continuously, including while the assistant is
talking.

The VAD's thresholds are tunable per-connection, live, without restarting the
server:

```json
{"type":"vad.config","speech_threshold":0.02,"silence_threshold":0.015,"min_speech_ms":100,"min_silence_ms":300}
```

Any subset of the four fields may be sent; omitted fields keep their current
value. The server replies with `vad.config.ack` (carrying the resulting full
config) or an `error` with `code: "INVALID_VAD_CONFIG"` if the update would
put `silence_threshold` at or above `speech_threshold`. The reference client's
"Voice activity detection" panel drives this message from range sliders and
shows a live mic-level meter against the current thresholds, so you can tune
barge-in sensitivity by ear instead of guessing at `.env` values.

`session.ready` includes the VAD's current config (under `vad`) so a client
can prefill its tuning UI with the server's real defaults rather than
duplicating them.

Server events include:

- `session.ready` (includes current `vad` config)
- `speech_started`
- `speaking`
- `speech_ended`
- `transcript`
- `assistant.token`
- `audio.start`
- `audio.chunk`
- `audio.end`
- `CLEAR_BUFFER`
- `generation.cancelled`
- `vad.config.ack`
- `error`

Audio packets are accompanied by generation metadata. The client must discard audio belonging to a stale generation.

## Architecture

```text
                         Application
                              |
                         VoiceAgent
                              |
                    +---------+---------+
                    | VoiceAgentPipeline|
                    +---------+---------+
                              |
              +---------------+----------------+
              |               |                |
             VAD             STT              State
                              |
                           transcript
                              |
                             LLM
                              |
                         TextChunker
                              |
                             TTS
                              |
                        Audio Stream
                              |
                         Transport
                    +---------+---------+
                    |                   |
                WebSocket          future WebRTC/SIP
```

## Concurrency: overlapped TTS calls

`TextChunker` splits the LLM's token stream into short phrases so TTS can
start before the full reply is generated. Network-backed TTS providers
(Gemini, Deepgram) go a step further with `core/concurrency.pipeline_ahead`:
while phrase N's audio is being sent to the client, phrase N+1's API call is
already running in the background, instead of the pipeline sitting idle
waiting for each call to finish before starting the next. Results still come
out in order. If a generation is cancelled mid-stream (barge-in), any
in-flight lookahead calls are cancelled too rather than left running to burn
API quota for audio nobody will hear. `voice_engine/core/concurrency.py` is a
generic `AsyncIterator[In] -> AsyncIterator[Out]` helper, reusable for any
other per-item async work a forked provider needs to overlap.

## Production hardening

The repository includes reference security controls and metrics, but deployment-specific authentication, TLS, proxy limits, provider credentials, persistence, and scaling policies must still be configured by the application owner.

## License

MIT
