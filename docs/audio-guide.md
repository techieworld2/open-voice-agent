# Audio tuning guide

[Back to the README](../README.md) · [WebSocket protocol](protocol.md)

Start with a mock session and headphones. Open **Microphone & processing** or **Voice activity & turns** as needed; controls have inline explanations and **?** help. Changes apply to the connected session. Provider changes in `.env` require a server restart.

## Hear the current input

1. Start the microphone and choose **Listen to my input** under **Listen & tune live**.
2. Select **Processed input** to hear the server-received audio after browser processing, input gain, telephony filters, wire decoding, and conversion to 16kHz mono PCM.
3. Toggle noise suppression, echo cancellation, or automatic gain. Check the actual device settings below the switches.
4. Select **VAD speech gate** to hear the monitor mute when server VAD detects no speech. Change thresholds and durations, and watch the live VAD badge.

The monitor uses a WebSocket round trip and its own volume. It works independently of STT, LLM, and TTS success. Gate preview changes local listening only: it does not rewrite STT input or captured samples. VAD controls detection and turn timing, rather than voice timbre.

**Capture & review** saves up to five seconds of the same server input in browser memory. That recording is a snapshot: later switch changes do not alter it. During local replay, outgoing caller audio is temporarily replaced with silence to avoid feeding the preview back into the agent.

## Microphone processing

| Setting | What it does | How to compare |
|---|---|---|
| Echo cancellation | Reduces speaker output picked up by the microphone | The effect depends on speaker leakage; headphones or a silent speaker may produce little difference. |
| Noise suppression | Reduces background noise, potentially changing soft speech too | Compare with a steady background sound present. |
| Automatic gain | Adjusts microphone loudness automatically | Watch RMS and perceived loudness; gain changes can alter VAD behaviour. |
| Input gain | Multiplies speech and noise after browser processing | Increase carefully; excess gain can clip audio and trigger VAD on noise. |

Desktop/headset, mobile/speakerphone, and raw presets request different combinations of browser processing. Raw requests all three browser DSP switches off. Device/OS processing can still affect the signal. Browser support varies; the actual device readout shows applied settings. [MDN explains echo cancellation constraints](https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackConstraints/echoCancellation).

Processing changes release the old mic before reacquiring it with the selected constraints. The WebSocket, audio context, and live monitor remain connected; there may be a brief input gap. Rapid changes coalesce to the latest selection. If a setting is rejected, the console attempts to restore the previous capture configuration and otherwise offers **Retry microphone**.

Microphone DSP does not apply to an uploaded file. File input accepts browser-decodable audio up to **25 seconds / 10MB** and streams it through the capture, profile, and codec pipeline. Capture review and uploads are not stored as files by the server.

## Understand VAD

The detector uses normalized RMS energy, separate speech/silence thresholds, and duration checks. The signal plot shows input RMS and the speech threshold. A higher threshold is not a noise-removal filter; it changes which sound is classified as speech.

| Control | Increase it to… | Tradeoff |
|---|---|---|
| Speech threshold | Reject more low-level noise before a turn begins | Quiet voices may be missed. |
| Silence threshold | Count more low-level sound as silence during a turn | Soft speech may end the turn prematurely. |
| Minimum speech duration | Reject clicks, coughs, and short bursts | The start of speech takes longer to confirm. |
| Minimum silence duration | Allow longer pauses before completing a turn | Replies start later. |
| Trailing-clause silence | Give unfinished clauses more thinking time | Incomplete utterances take longer to resolve. |
| Barge-in speech duration | Resist accidental interruptions during agent playback | Intentional interruptions take longer to confirm. |

The silence threshold stays below the speech threshold to prevent rapid switching. Editing either slider automatically adjusts the other when needed to keep the pair valid. Acoustic durations round up to a 20ms frame boundary: a selected 350ms pause is observed at 360ms.

VAD presets cover quiet rooms, balanced desktop audio, mobile/noisy environments, and narrowband phone acoustics. Start with a preset, then tune for the actual microphone and room. **Reset VAD to server defaults** restores the selected profile's original configuration.

Edits stream to the server while dragging. A correlated acknowledgement displays the applied revision; older acknowledgements cannot overwrite newer edits. Silence changes also wake an already pending semantic turn deadline. The live VAD badge shows speech or silence from server decisions. VAD edits keep the same microphone track and WebSocket.

## Turn detection and interruptions

### Semantic end of turn

The heuristic combines energy VAD with transcript endings. Trailing words `and`, `or`, `but`, `because`, `so`, `if`, `um`, `uh`, `like`, and `actually` extend the silence window. Trailing articles, prepositions, and continuation punctuation also extend it.

| Ending | Wideband | Telephony |
|---|---:|---:|
| Complete clause / terminal punctuation | 300ms | 350ms |
| Trailing or incomplete clause | 950ms | 1100ms |

These are profile defaults. Custom base and trailing durations persist across transcripts; the trailing duration never shortens the base window. Disable **Semantic end of turn** for a fixed manual silence window. This is an explainable lexical heuristic, rather than a trained semantic classifier.

Streaming recognizers can revise the deadline from partial transcripts. Batch faster-whisper waits for the first acoustic end, transcribes, then delays generation to a revised semantic deadline when needed. If the caller resumes during that extension, text is retained and merged. The final transcript remains visible even when an LLM/TTS request fails.

### Passive backchannels

During agent generation or pending browser playback, interruption requires at least **320ms of voiced sound** and a non-passive transcript. The console can increase that minimum, but cannot set it below 320ms.

Single passive responses such as “uh-huh,” “yeah,” “yep,” “okay,” “right,” and “mhm” alone keep playback running by default. Shorter bursts are ignored. With a batch recognizer, confirmation waits for the final transcript; that recognition delay is part of the detection cost. Disabling the passive-backchannel filter retains the minimum duration requirement.

### Spoken context

Sending audio is not proof of playback. The player reports rendered sample watermarks for each generation. A confirmed interruption cancels active generation, invalidates pending output, and sends `barge_in_clear`. The browser flushes its queue and acknowledges the final watermark. The server retains the assistant's confirmed spoken prefix and emits `context.sync` for the transcript's red strikethrough display.

Word-level retention requires provider-supplied `AudioChunk.word_end_samples`; mock TTS supplies this alignment. Providers without timestamps use conservative completed-phrase boundaries. Exact words inside an unaligned phrase cannot be inferred from audio duration. Render acknowledgements exclude physical output-device latency; clients without acknowledgements retain no confirmed spoken context.

Before the next LLM call, the pipeline allows up to 250ms for a final flush acknowledgement. Missing acknowledgements retain the conservative prefix. Cancelling a coroutine cannot stop blocking provider work already running in a worker thread, but invalidated generations cannot emit its results.

## Playback and device profiles

Prebuffering trades additional audible startup delay for smoother playback. It is outside the server's TTFA measurement. Pause holds queued agent audio without acknowledging unplayed samples. A **10-second queue cap** clears and interrupts an overfilled response. **Flush playback** also cancels generation and synchronizes history.

Leave the sample-rate assumption on **Auto** for normal playback. A forced assumption deliberately changes pitch and speed; it does not change the microphone profile or audio sent to STT.

Wideband sends 320 PCM samples per 20ms packet at 16kHz. Telephony sends 160 samples at 8kHz, with 300Hz high-pass and 3400Hz low-pass filters on both microphone and speaker paths. Capture retains fractional resampling phase across render blocks, including 44.1kHz devices. Provider STT always receives 16kHz PCM; phone-bandwidth detail remains narrowband after upsampling.

Profile/codec changes currently require reconnecting. Mobile presets retain the WebSocket transport. A physical phone requires HTTPS and an allowed browser origin for microphone access; `localhost` on that phone points to the phone itself.
