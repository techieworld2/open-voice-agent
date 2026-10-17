# Native FreeSWITCH call lab

The optional lab uses FreeSWITCH on Windows for SIP registration and RTP audio. Your Python voice engine runs on Windows too; Docker and Ubuntu are not required. The separate dashboard is **http://localhost:8000/call-lab**, sharing the theme and navigation of the original WebSocket console at **http://localhost:8000/**.

```text
Linphone / desktop or mobile softphone
          |
     SIP + G.711 PCMU RTP, 8kHz / 20ms
          |
     Native Windows FreeSWITCH
     account 1001 / extension 7000
          |
     Private loopback SIP + RTP
          |
     Python gateway + RTP network presets
          |
     Existing VAD / STT / semantic EoT / LLM / TTS
          |
     /call-lab dashboard: live controls, transcript, latency
```

The adapter uses stock `mod_sofia` and `mod_event_socket`, plus a bounded local Python gateway. It needs no `mod_audio_stream`, commercial streaming module, or custom C++ FreeSWITCH module. Existing provider interfaces are unchanged.

## Setup

Install a **trusted native Windows FreeSWITCH build** first. The official project documents Windows builds and links its Windows installer directory in [its README](https://github.com/signalwire/freeswitch#readme). Source builds require Microsoft's C++ build tools and FreeSWITCH dependencies. This repository does not bundle an installer or assert that an arbitrary old Windows binary is current.

From the repository root, with the Python virtual environment active:

```powershell
python -m tools.setup_freeswitch_lab --host 192.168.1.45
python -m tools.run_freeswitch_lab --freeswitch-dir "C:\Program Files\FreeSWITCH"
```

Replace the example IP with your computer's **LAN IPv4 address** from `ipconfig`. Supply the actual FreeSWITCH directory or executable path. Stop an existing server using port 8000 first. The launcher starts only its own FreeSWITCH process and stops it when the Python server exits.

If a Windows FreeSWITCH service already occupies the default ports, generate the lab with separate ports instead:

```powershell
python -m tools.setup_freeswitch_lab --host 192.168.1.45 --sip-port 5062 --loopback-port 5092 --esl-port 8022 --rtp-start 40000
```

Then register the phone on UDP **5062**. This configuration uses RTP UDP **40000–40127** and loopback management **8022**. The dashboard displays the configured SIP port. Choose ports when generating the configuration; existing credential files are preserved.

Setup creates isolated configuration, logs, and database directories under `call-lab/freeswitch/`. It prints the SIP password and separate dashboard token and preserves existing credentials on subsequent runs. It does not overwrite FreeSWITCH's installation configuration, install a Windows service, or modify the original `.env`. Generated files are excluded from Git and Docker build contexts.

Open **http://localhost:8000/call-lab** and enter the printed dashboard token under **Connect a phone**. Health status distinguishes a listening Python gateway from a connected FreeSWITCH server with running SIP profiles. Configuration existing on disk is not treated as proof of a running server.

Use `--check` to validate local files without starting processes. Use `--engine-only` to run the dashboard and gateway while starting FreeSWITCH separately, or to inspect its unavailable status:

```powershell
python -m tools.run_freeswitch_lab --freeswitch-dir "C:\Program Files\FreeSWITCH" --check
python -m tools.run_freeswitch_lab --engine-only
```

## Connect a softphone

Install [Linphone](https://www.linphone.org/en/download/) or another SIP softphone and select an existing SIP account:

| Setting | Value |
|---|---|
| Username / authentication ID | `1001` |
| SIP domain / identity domain | Your computer's LAN IPv4 address, without a port |
| Registrar URI | `sip:YOUR_LAN_IP:PORT` |
| Port / transport | `CALL_LAB_SIP_PORT` in the generated file (`5060` by default) / UDP |
| Password | `CALL_LAB_SIP_PASSWORD` in `call-lab/freeswitch/lab.env` |
| Codec | PCMU / G.711 μ-law, 8000Hz |
| Number to dial | `7000` |

On mobile, the phone and computer need network access to each other. Use the computer's LAN IP, not `localhost`. Windows Firewall may need private-network rules for FreeSWITCH's UDP 5060 and RTP UDP 16384–16511. Do not forward these lab ports from your router to the public internet.

When Linphone and the default FreeSWITCH service run on the same Windows computer, both can bind local UDP 5060. Registration replies can then reach the wrong process. Select **TCP** in Linphone and use `sip:YOUR_LAN_IP:PORT;transport=tcp` as Registrar URI; the generated FreeSWITCH profile accepts TCP as well. Leave the outbound proxy empty. Call audio continues over RTP. Alternatively, configure Linphone to use a different local UDP port.

FreeSWITCH authenticates the tester and routes `7000` through its `engine-loopback` profile to Python. Engine-facing SIP port 5070, RTP port 5072, and management port 8021 bind to **127.0.0.1**. Initial INVITEs require the generated token; subsequent dialog IDs/tags and the negotiated media source are checked. The gateway is a narrow loopback integration, not a general-purpose public SIP server.

The configuration starts with one tester account. Add distinct FreeSWITCH directory accounts for independent users. Registration belongs to FreeSWITCH; the dashboard shows engine call sessions rather than a registration directory.

## Live controls and providers

Select a call to tune VAD thresholds, speech/silence durations, semantic EoT, trailing-clause timing, backchannel filtering, and transcription-only mode without hanging up. Each call has an independent pipeline and conversation. The dashboard can end the selected call. The gateway supports initial INVITE/ACK, BYE, repeated offers, and basic hold/resume re-INVITEs for this PCMU profile; it does not implement every SIP extension.

Open **Listen & capture input** to hear the selected caller's received STT input live, switch to a VAD-gated preview, adjust preview volume/buffering, or capture and replay up to five seconds. G.711 is decoded and resampled to the same 16kHz PCM format supplied to STT; resampling does not restore frequencies lost in telephony audio. VAD gating changes the preview only. Captures stay in page memory and are not uploaded or stored on disk. Use headphones to prevent preview feedback into the call.

The authenticated monitor WebSocket copies received audio to the dashboard; it does not carry the SIP call or capture the browser microphone. Preview queues are bounded and slow observers do not block the call. Echo cancellation, noise suppression, device selection, and handset playback buffering belong to Linphone or the phone's audio settings. Their effects on transmitted microphone audio can be heard in the received-input preview.

Transcripts appear independently of LLM success. Mock defaults require no paid keys: STT returns deterministic text and TTS plays tones. For intelligible conversation, edit provider choices in `call-lab/freeswitch/lab.env`, install their optional extras, and restart:

```dotenv
VOICE_ENGINE_STT=faster_whisper
VOICE_ENGINE_LLM=gemini
VOICE_ENGINE_TTS=deepgram
```

```powershell
python -m pip install -e ".[whisper,gemini,deepgram,mulaw]"
```

Keys and provider-specific settings can remain in the original `.env`; lab provider choices take precedence through the launcher. Windows paths to local Whisper models work because Python runs on Windows. Python 3.13+ needs `audioop-lts` for G.711; the `mulaw` and `dev` extras include it conditionally.

## RTP network presets

Presets affect actual packets in both directions of the **FreeSWITCH-to-Python RTP leg**. They do not alter the phone-to-FreeSWITCH leg or browser microphone sessions. They apply to all FreeSWITCH lab calls and need no privileged Linux networking tool.

| Preset | Added one-way delay | Jitter | Random packet loss |
|---|---:|---:|---:|
| Clean | 0ms | 0ms | 0% |
| Mobile-like | 80ms | ±20ms | 1% |
| Unstable | 160ms | ±60ms | 5% |

These are repeatable parameters with stochastic packet patterns, not measured carrier statistics. The Python receive path has a small resequencing buffer and substitutes G.711 silence for missing frames; it is not a carrier-grade PLC algorithm. FreeSWITCH and the phone have their own buffering and codec behavior. RTP events report received/sent packets, simulated drops, and concealed input frames.

## Interruption and timing

Confirmed barge-in cancels generation and drops unsent audio and delayed outgoing packets. RTP audio already handed to FreeSWITCH or the handset cannot be recalled.

**Context retention uses a conservative RTP transmission estimate, not a handset acknowledgement.** `VOICE_ENGINE_FREESWITCH_PLAYOUT_MARGIN_MS` defaults to 200ms. The estimate combines packet send times with TTS word/phrase alignment; it cannot guarantee the exact words heard under variable latency or loss. The dashboard labels this basis and highlights the retained prefix and discarded suffix.

TTFA ends at the pipeline's first generated audio frame. `barge_in_cancel_latency_ms` measures the interruption gate to its cancellation event. `gateway_queue_clear_ms` measures clearing local RTP output queues. These are not physical speaker measurements, and the adapter does not fabricate FreeSWITCH queue-drained acknowledgements.

## Validation

Regular tests require neither FreeSWITCH nor paid keys:

```powershell
python -m pytest -q
```

Adapter tests exchange actual UDP SIP/RTP with a simulated FreeSWITCH peer. They cover negotiation, credentials, retransmission, hold/resume, frame handling, mock turns, interruption/history, source validation, network profiles, XML generation, and event-socket probing. They do not establish that a particular native FreeSWITCH binary works.

The native integration was also verified on Windows with FreeSWITCH **1.11.3**: SIP registration, an answered call to `7000`, voiced G.711 return audio, and barge-in cancellation with mock providers. The UDP receive loop recovers from Windows destination-unavailable errors so a brief startup race or an expired peer does not permanently stop receiving audio.

With FreeSWITCH running on mock providers, run:

```powershell
python -m tools.sip_probe
$env:VOICE_ENGINE_RUN_SIP_TESTS="1"
python -m pytest tests/test_call_lab_live.py -q
```

The smoke caller registers as `1001`, dials `7000`, sends G.711 tones, receives voiced RTP, injects a barge-in, checks cancellation metrics, and hangs up. Run while no other tester is starting calls. It tests signaling/audio transport, not ASR accuracy. The live pytest test is skipped unless enabled.

## Dashboard APIs

APIs remain under `/api/call-lab`, including per-call `/configure` and `/hangup` actions and `/network` presets. They honor the dashboard token and allowed origins. Up to 20 call records stay in server memory, each with 60 transcript rows and 200 recent events; restarting clears them. Use one Uvicorn worker so the dashboard and calls share state.

The original `/ws/voice` microphone connection is unchanged.
