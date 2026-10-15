# Open Voice Engine

A WebSocket voice runtime for experimenting with speech recognition, conversational models, and streaming speech synthesis. Connect a microphone, tune turn detection while speaking, inspect interruptions, and measure concurrent calls from one browser console.

**Python 3.11+ · Pure WebSockets · Zero-build browser console · Key-free mock defaults**

[Quick start](#quick-start) · [Browser console](#browser-console) · [Providers](#providers) · [Architecture](#architecture) · [Benchmarks](#benchmarks) · [Documentation](#documentation)

| Capability | What you can test |
|---|---|
| Two audio profiles | 16kHz wideband or 8kHz phone acoustics, both in paced 20ms WebSocket frames |
| Live audio tuning | Microphone processing, VAD thresholds, speech/silence durations, and input monitoring |
| Turn handling | Semantic silence windows, passive backchannel filtering, and playback-aware interruption history |
| Provider isolation | Local transcription stays visible when an LLM or TTS provider fails |
| Concurrency profiling | Browser, API, and CLI reports for TTFA, complete-turn latency, and barge-in cancellation |

## Quick start

Run from the repository directory with Python 3.11 or newer. The default configuration uses mocks for every provider; no API keys, model downloads, or frontend build are required.

<details open>
<summary><strong>Windows PowerShell</strong></summary>

```powershell
git clone https://github.com/techieworld2/open-voice-agent.git
cd open-voice-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn voice_engine.server.app:app --host 127.0.0.1 --port 8000
```

Using the environment's Python directly avoids needing to change PowerShell's activation policy.

</details>

<details>
<summary><strong>Linux / macOS</strong></summary>

```bash
git clone https://github.com/techieworld2/open-voice-agent.git
cd open-voice-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
voice-engine dev --host 127.0.0.1 --port 8000
```

</details>

Open **[localhost:8000](http://localhost:8000)** or **[127.0.0.1:8000](http://127.0.0.1:8000)**, allow microphone access, and choose **Start microphone**. The guided tour explains the controls; **Skip tour** and **Esc** dismiss it, and **Help & tour** replays it.

Mock STT returns deterministic text and mock TTS plays synthetic tones. Select real providers when you want natural conversation. You can also submit a text turn to exercise LLM → TTS without speaking.

The server reads `.env` from its working directory. Existing process environment variables take precedence. Restart Python after changing provider configuration or backend code; browser audio and VAD controls apply to the connected session. After frontend updates, use **Ctrl+F5** to refresh the console.

## Browser console

![Open Voice Engine console with collapsed settings, pipeline stages, and independent transcription](docs/images/console.png)

The four settings panels start collapsed. Technical controls have **?** explanations on hover, keyboard focus, or tap.

| Goal | Where to start |
|---|---|
| Try local STT without an LLM | **Session → Transcription only**. Speech remains visible even if reply generation is unavailable. |
| Hear microphone processing live | **Listen & tune live → Listen to my input**, with **Processed input** selected. Use headphones. |
| Hear VAD decisions | Select **VAD speech gate** as the monitor source, then change thresholds or durations while speaking. |
| Compare echo/noise processing | Toggle controls in **Microphone & processing**. Capture refreshes while the WebSocket stays connected. |
| Tune turn taking | **Voice activity & turns**. The live VAD badge and server revision confirm what is active. |
| Reproduce an audio test | Choose an audio file as the session source, or save a five-second input capture for replay. |
| Inspect interruptions | Watch the spoken prefix and red strikethrough text in the conversation panel. |

**VAD changes detection and timing; it does not change voice timbre.** The gate preview makes detection audible. A saved capture is a fixed snapshot, so use the live monitor to compare later changes. Switching between the 16kHz and 8kHz transport profiles currently requires reconnecting the session.

For speech/silence definitions, tuning examples, browser processing caveats, and playback controls, see the **[audio tuning guide](docs/audio-guide.md)**.

## Audio profiles

| Profile | Rate | Codec | Samples / frame | Bytes / frame | Complete / trailing silence |
|---|---:|---|---:|---:|---:|
| Wideband | 16,000Hz | PCM s16le | 320 | 640 | 300 / 950ms |
| Phone simulation | 8,000Hz | PCM s16le | 160 | 320 | 350 / 1100ms |
| Phone simulation | 8,000Hz | G.711 μ-law | 160 | 160 | 350 / 1100ms |

Every frame represents **20ms**. Phone simulation applies a **300–3400Hz bandpass** on browser microphone and speaker paths. The server converts STT input to 16kHz mono PCM; upsampling does not restore frequencies removed by the phone profile.

Connect to `/ws/voice?mode=wideband` or `/ws/voice?mode=telephony&codec=mulaw`. Omitted parameters use `.env` defaults. This is phone-acoustic simulation over WebSockets; no WebRTC or SIP transport is involved.

PCM needs no codec extra. Python 3.11/3.12 includes `audioop`; for μ-law on Python 3.13+, install `python -m pip install -e ".[mulaw]"`.

## Providers

The [provider registry](src/voice_engine/providers/registry.py) keeps STT, LLM, and TTS interchangeable through their existing interfaces.

| Role | Provider setting | Optional install | Requirements |
|---|---|---|---|
| All | `mock` | Base installation | No model files or API keys |
| STT | `faster_whisper` | `.[whisper]` | Local Whisper model; defaults to the smaller multilingual `base` model |
| LLM / TTS | `gemini` | `.[gemini]` | `GEMINI_API_KEY` or `GOOGLE_API_KEY`, and access to the configured model |
| LLM | `openai_completions` | `.[openai_completions]` | Compatible `/v1/completions` server, such as vLLM or Ollama |
| TTS | `kokoro` | `.[kokoro]` | Local Kokoro dependencies and models |
| TTS | `deepgram` | `.[deepgram]` | `DEEPGRAM_API_KEY` |

Provider settings belong in `.env`. The committed [.env.example](.env.example) stays key-free:

```dotenv
VOICE_ENGINE_STT=mock
VOICE_ENGINE_LLM=mock
VOICE_ENGINE_TTS=mock
VOICE_ENGINE_MODE=wideband
```

For local Whisper plus Gemini and Deepgram, install their extras:

```bash
python -m pip install -e ".[whisper,gemini,deepgram]"
```

Then set `VOICE_ENGINE_STT=faster_whisper`, `VOICE_ENGINE_LLM=gemini`, and `VOICE_ENGINE_TTS=deepgram` in `.env`, along with your keys and available model names. Restart Python after saving. Use **Transcription only** to test local recognition without making LLM/TTS requests.

Whisper downloads on first use unless its model is already cached. To download it before starting the server:

```bash
hf download Systran/faster-whisper-base --local-dir models/whisper-base --max-workers 1
```

Set `VOICE_ENGINE_WHISPER_MODEL=./models/whisper-base` after the download completes. Model initialization is serialized across threads. Faster-whisper buffers an utterance and transcribes after the pause; it does not emit word-by-word live partials. [Troubleshooting](#troubleshooting) covers downloads and quotas.

For vLLM/Ollama, configure `VOICE_ENGINE_LLM_URL` and `VOICE_ENGINE_LLM_MODEL` for the server's compatible completion endpoint. Custom providers can follow [examples/custom_provider.py](examples/custom_provider.py).

## Architecture

```text
 Browser microphone                                    Browser speaker
        |                                                     ^
 Browser DSP + phone bandpass                         Phone bandpass
        |                                                     ^
 Capture AudioWorklet                                 Playback AudioWorklet
 resample + 20ms frames                               queue + sample ACKs
        |                                                     ^
        +================== WEBSOCKET ========================+
                            |                       ^
                     decode + resample              |
                            |                       |
                    Energy VAD + STT                |
                            |                       |
                     semantic EoT                   |
                            |                       |
                       LLM registry                 |
                            |                       |
                      phrase chunker                |
                            |                       |
                       TTS registry ----> resample + frame + pace

 Confirmed barge-in --> cancel generation --> barge_in_clear --> flush player
 Playback watermark --> truncate assistant history --> context.sync

 Benchmark API / CLI --> concurrent WebSocket callers --> latency percentiles
```

Trailing connectors such as “and” or “because” extend the silence window. Brief bursts and passive backchannels are filtered before interrupting an agent response. The detector uses explainable lexical heuristics alongside energy VAD. [Turn detection and interruption details](docs/audio-guide.md#turn-detection-and-interruptions) explain the batch-STT timing and playback guarantees.

**History precision depends on TTS alignment.** Aligned providers can retain completed words; providers without timestamps retain completed phrases conservatively. Mock TTS supplies word boundaries. Render acknowledgements do not measure physical speaker latency.

## Benchmarks

With the server running, use the browser's **Run 30-Call Concurrency Benchmark** button or the dependency-free CLI:

```bash
python -m benchmarks.stress_tester --concurrency 30 --mode telephony
python -m benchmarks.stress_tester --concurrency 30 --mode wideband --json
```

The installed-package entry point is `python -m voice_engine.benchmarks.stress_tester`. The API is `GET /api/benchmark?concurrency=30&mode=telephony`; it admits one local benchmark at a time and checks capacity. Supply `Authorization: Bearer ...` when server authentication is enabled. The CLI also accepts `--url` and `--token`.

Each caller sends paced voiced tones, completes one response, and interrupts a second response. These probes exercise transport, VAD, and mock recognition; they do not measure real STT accuracy. Use intelligible recorded speech for real recognition benchmarks. Failures are reported separately and excluded from percentile calculations; empty distributions display `n/a`.

Example table — **illustrative values, not measured results or a performance guarantee**:

| Metric (ms) | P50 | P95 | P99 | Max |
|---|---:|---:|---:|---:|
| Time to first audio | 550.0 | 565.0 | 570.0 | 575.0 |
| Complete-turn latency | 4150.0 | 4250.0 | 4290.0 | 4300.0 |
| Barge-in cancellation | 65.0 | 145.0 | 150.0 | 155.0 |

TTFA starts at the last voiced input frame. Complete-turn latency ends at `audio.end`. Benchmark cancellation starts at the 320ms interruption gate and ends when `barge_in_clear` arrives. Caller timings include transport; speaker hardware latency and browser prebuffer delay are outside these measurements.

## Development

```bash
python -m pytest -q
# Optional Node checks; no npm install or frontend build:
node --test tests/worklet.test.cjs tests/console.test.cjs
```

Core tests use mocks and require no paid API keys. Tests cover semantic deadlines, live configuration, backchannels, history truncation, audio profiles, concurrent calls, and provider failure isolation. Node checks exercise Worklet framing/playback and console configuration/gate routing. Optional provider SDK tests may skip until their extras are installed.

Docker uses the same mock defaults:

```bash
docker compose up --build
```

Compose loads `.env` when present; the optional `env_file` configuration requires Compose 2.24+. Install provider extras in a custom image when selecting real providers. Local keys, models, and virtual environments are excluded from the build context.

## Troubleshooting

| Symptom | Check |
|---|---|
| WebSocket handshake returns 403 | Allow the page's exact origin in `VOICE_ENGINE_ALLOWED_ORIGINS`; supply the session token if configured. Both local browser addresses are allowed by default. |
| Speech starts/ends, but no transcript | Check STT stage logs and model readiness. Whisper must finish downloading/loading and then finish utterance inference. |
| Hugging Face returns 429 | Retry the download later, use the smaller `base` model, or point to a completed local model directory. Successful metadata requests do not mean weights are ready. |
| Gemini quota is exhausted | Select **Transcription only**. STT remains active and existing transcripts stay visible. |
| VAD changes sound identical | VAD changes detection, not timbre. Choose **VAD speech gate** and watch the live server badge. |
| Old settings return while dragging | Refresh the console. If it reports an older backend, restart Python once to load correlated live configuration. |
| Noise/echo switches sound similar | Compare noise suppression with background noise present. Echo cancellation depends on speaker leakage; inspect the actual device settings. |
| A phone cannot access its microphone | Serve over HTTPS and allow its browser origin. `localhost` on the phone refers to that phone. |

For a public deployment, configure authentication, exact origin allowlists, connection limits, and TLS termination. Blocking provider work already running in a worker thread can continue after coroutine cancellation, while invalidated output is discarded.

## Documentation

- [Audio tuning guide](docs/audio-guide.md) — VAD definitions, live listening, presets, semantic turns, and interruption precision.
- [WebSocket protocol](docs/protocol.md) — connection profiles, controls, audio framing, events, and metric definitions.
- [Environment reference](.env.example) — provider models, keys, endpoints, and server defaults.
- [Custom provider example](examples/custom_provider.py) · [Contributing](CONTRIBUTING.md)

Released under the [MIT license](LICENSE).
