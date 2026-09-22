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

Open `http://localhost:8000`. The default providers are mock providers, so no API key is required.

## Switching providers

The server picks providers via environment variables (see `.env.example`):

```text
VOICE_ENGINE_STT=mock
VOICE_ENGINE_LLM=mock
VOICE_ENGINE_TTS=mock
```

## Kokoro TTS

```bash
pip install -e ".[kokoro]"
```

```text
VOICE_ENGINE_TTS=kokoro
```

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

`GeminiTTS` generates 24kHz mono PCM audio. `GeminiLLM` streams response tokens using Gemini's async chat API. Both utilize `GEMINI_API_KEY`.

## Deepgram Aura TTS

```bash
pip install -e ".[deepgram]"
```

```text
VOICE_ENGINE_TTS=deepgram
DEEPGRAM_API_KEY=your-key-here
VOICE_ENGINE_DEEPGRAM_TTS_MODEL=aura-2-thalia-en
```

`DeepgramTTS` streams 24kHz linear16 PCM via Deepgram's `/v1/speak` endpoint.

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

`FasterWhisperSTT` transcribes speech utterances buffered between VAD events in a worker thread (`asyncio.to_thread`). Model instances are cached per process.

- **CPU Performance**: Use `VOICE_ENGINE_WHISPER_MODEL=large-v3-turbo` for faster CPU inference.
- **GPU Acceleration**: Set `VOICE_ENGINE_WHISPER_DEVICE=cuda` if your environment supports CUDA.
- **Noise Filtering**: Combines Silero VAD filtering with `no_speech_prob` and `avg_logprob` thresholds to prevent non-speech audio hallucinations.

## Generic OpenAI-compatible LLM (vLLM, LM Studio, etc.)

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

`OpenAICompletionsLLM` streams tokens from any server supporting the OpenAI `/v1/completions` API format (vLLM, LM Studio, Ollama, llama.cpp).

## Sample-rate Simulator (Reference Client)

The reference browser client (`static/index.html`) includes a sample-rate selection tool to test playback resampling across different audio hardware configurations.

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

## WebSocket Protocol

Clients connect and initiate a session with:

```json
{"type":"session.start","session_id":"demo","sample_rate":16000,"channels":1,"encoding":"pcm_s16le"}
```

Audio frames are streamed as binary signed 16-bit little-endian mono PCM.

### Interruption (Barge-in)
Barge-in is automatic: when VAD detects user speech (`speech_started`) while the assistant is processing or speaking, the active generation pipeline is instantly cancelled.

### Dynamic VAD Configuration
VAD thresholds can be adjusted dynamically per session:

```json
{"type":"vad.config","speech_threshold":0.02,"silence_threshold":0.015,"min_speech_ms":100,"min_silence_ms":300}
```

The server responds with `vad.config.ack` or an `error` message.

### Server Events
- `session.ready`
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

## Concurrency & Pipeline Optimization

`TextChunker` splits LLM token output into short phrases for fast initial TTS playback. For network-backed TTS providers (Gemini, Deepgram), `pipeline_ahead` pre-fetches audio for upcoming text phrases in parallel while current audio chunks stream to the client.

## Security & Deployment

Production deployment requires configuring environment-specific authentication tokens, TLS termination, CORS origin allowlists, connection limits, and rate limits.

## License

MIT
