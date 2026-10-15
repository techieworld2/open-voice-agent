"use strict";
const el = id => document.getElementById(id);
let ws, stream, context, source, capture, player, inputGain, outputGain, fileSource, previewSource, monitorPlayer, monitorGain;
let generation = null, metadata = null, encoding = "pcm_s16le";
let ready = false, starting = false, closing = false, inputPaused = false, playbackPaused = false;
let runId = 0, currentUtterance = 0, serverVad = null, vadTimer;
let vadRequestId = 0, vadEditVersion = 0, pendingVad = null, lastVadStatus = null;
let recording = false, recordedFrames = [], recordedRate = 16000, recordingSamples = 0;
let recordedClip = null, localPreview = false, audioFileBuffer = null;
let monitoring = false, monitorSamples = 0, captureChanges = Promise.resolve();
let captureRevision = 0, captureRefreshing = false, lastCaptureSettings = null;
const rows = new Map(), blockedGenerations = new Set(), levels = [];
let logLines = [], drawScheduled = false;
const metric = value => `${Number(value).toFixed(2)} ms`;
const capturePresets = {
  headset: {echoCancellation: true, noiseSuppression: true, autoGainControl: false, help: "Echo cancellation and noise suppression, with automatic gain disabled for a consistent input level."},
  mobile: {echoCancellation: true, noiseSuppression: true, autoGainControl: true, help: "Speakerphone processing: echo cancellation, noise suppression, and automatic gain. Tune VAD after the browser adjusts the level."},
  raw: {echoCancellation: false, noiseSuppression: false, autoGainControl: false, help: "Requests unprocessed microphone audio for measurement. Use headphones to keep speaker audio out of the microphone."},
};
const vadFields = {speech: "speech_threshold", silence: "silence_threshold", minSpeech: "min_speech_ms", minSilence: "min_silence_ms", trailingSilence: "trailing_silence_ms", bargeSpeech: "barge_in_min_speech_ms"};
const vadPresets = {
  quiet: {speech_threshold: .012, silence_threshold: .008, min_speech_ms: 80, min_silence_ms: 300, trailing_silence_ms: 950, barge_in_min_speech_ms: 320, semantic_eot: true, filter_backchannels: true},
  balanced: {speech_threshold: .020, silence_threshold: .015, min_speech_ms: 100, min_silence_ms: 300, trailing_silence_ms: 950, barge_in_min_speech_ms: 320, semantic_eot: true, filter_backchannels: true},
  mobile: {speech_threshold: .030, silence_threshold: .020, min_speech_ms: 180, min_silence_ms: 500, trailing_silence_ms: 1200, barge_in_min_speech_ms: 400, semantic_eot: true, filter_backchannels: true},
  phone: {speech_threshold: .025, silence_threshold: .018, min_speech_ms: 120, min_silence_ms: 350, trailing_silence_ms: 1100, barge_in_min_speech_ms: 320, semantic_eot: true, filter_backchannels: true},
};
const vadDescriptions = {server: "Starts with the selected transport’s server configuration.", quiet: "A lower noise floor for a close microphone in a quiet room.", balanced: "Responsive turn taking for a desktop microphone or headset.", mobile: "More sustained speech and longer pauses for a variable mobile or noisy-room signal.", phone: "Thresholds and silence windows tuned for narrowband phone acoustics.", custom: "Your own thresholds and durations. Changes apply to this session."};
function log(message) {
  const stamp = new Date().toLocaleTimeString([], {hour12: false});
  logLines.push(`${stamp}  ${message}`); if (logLines.length > 300) logLines.shift();
  el("log").textContent = logLines.join("\n"); if (el("autoScroll").checked) el("log").scrollTop = el("log").scrollHeight;
}
function send(message) { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message)); }
function activity(message, state = "active") { el("status").textContent = message; el("activityDot").dataset.state = state; }
function notice(message) { el("notice").textContent = message; el("notice").hidden = !message; }
function connection(state) {
  el("connection").dataset.state = state;
  el("connectionText").textContent = {live: "Connected", connecting: "Connecting", idle: "Disconnected"}[state];
}
function wireRate() { return el("mode").value === "telephony" ? 8000 : 16000; }
function profileDisplay() {
  const phone = el("mode").value === "telephony";
  el("profile").textContent = phone ? "8kHz Telephony WS" : "16kHz Wideband WS";
  if (!phone) el("codec").value = "pcm_s16le";
  el("codec").disabled = ready || starting || !phone;
  el("profileDetail").textContent = `${el("codec").value === "mulaw" ? "G.711 μ-law" : "PCM"} · mono · 20ms frames`;
  el("modeHelp").textContent = phone ? "300–3400Hz bandpass on microphone and speaker. 160 samples per 20ms packet." : "Full-spectrum audio. 320 samples in each 20ms packet.";
  if (!ready) el("eot").textContent = `${phone ? 350 : 300}ms Complete`;
}
function updateControls() {
  const busy = ready || starting, file = el("inputSource").value === "file";
  el("start").disabled = busy; el("stop").disabled = !busy;
  for (const id of ["mode", "inputSource", "micDevice"]) el(id).disabled = busy;
  el("codec").disabled = busy || el("mode").value !== "telephony";
  el("start").textContent = file ? "Connect session" : "Start microphone"; el("fileControls").hidden = !file;
  for (const id of ["sendText", "pausePlayback", "flushPlayback", "pauseInput", "recordClip"]) el(id).disabled = !ready;
  el("sendText").disabled = !ready || el("responseMode").value === "transcription";
  el("monitorInput").disabled = !ready;
  el("streamFile").disabled = !ready || !file || !audioFileBuffer || Boolean(fileSource);
  el("playClip").disabled = !ready || !recordedClip || recording;
}
function stage(message) {
  const {stage: name, status, utterance_id: utterance, generation_id: id} = message;
  if (id && blockedGenerations.has(id)) return;
  if (name === "stt" && utterance) { if (utterance < currentUtterance) return; currentUtterance = utterance; }
  el(`stage-${name}`).dataset.state = status;
  const labels = {capturing: "Listening", processing: name === "stt" ? "Loading / recognizing" : "Processing", streaming: "Streaming", completed: "Complete", empty: "No speech recognized", cancelled: "Superseded", failed: "Error"};
  el(`${name}State`).textContent = labels[status] || status;
  if (name === "stt" && status === "processing") activity(`Recognizing speech${message.model ? ` · ${message.model}` : ""}. First use may load the model.`);
  if (name === "stt" && status === "capturing") activity("Listening to microphone…");
  if (name === "stt" && status === "empty") activity("No speech text recognized. Check the input signal and try again.", "idle");
  if (name === "llm" && status === "processing") activity("Generating the agent’s reply…");
  if (name === "tts" && status === "processing") activity("Synthesizing reply audio…");
  if (name === "tts" && status === "streaming") activity("Streaming agent audio to playback…");
  if (name === "tts" && status === "completed") activity("Reply delivered · listening", "idle");
}
function row(role, id) {
  if (id && rows.has(id)) return rows.get(id); el("transcriptEmpty")?.remove();
  const container = document.createElement("div"); container.className = "transcript-row"; container.dataset.role = role === "Agent" ? "agent" : "caller";
  const meta = document.createElement("div"); meta.className = "message-meta";
  const heading = document.createElement("strong"); heading.textContent = role;
  const timestamp = document.createElement("time"); timestamp.textContent = new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  const text = document.createElement("div"); text.className = "message-body";
  meta.append(heading, timestamp); container.append(meta, text); el("transcript").append(container);
  const result = {container, text}; if (id) rows.set(id, result);
  if (el("transcript").children.length > 200) {
    const removed = el("transcript").firstElementChild; removed.remove();
    for (const [key, item] of rows) if (item.container === removed) rows.delete(key);
  }
  el("transcript").scrollTop = el("transcript").scrollHeight; return result;
}
function syncTranscript(message) {
  const item = row("Agent", message.generation_id); item.text.replaceChildren(document.createTextNode(message.spoken_text || ""));
  const discarded = document.createElement("span"); discarded.className = "discarded"; discarded.textContent = ` ${message.discarded_text || ""}`;
  const note = document.createElement("span"); note.className = "interrupted"; note.textContent = `[Interrupted — Truncated from LLM History] · ${message.precision || "phrase"} boundaries`;
  item.text.append(discarded, note);
}
function bandpass(input, destination, ac = context) {
  if (el("mode").value !== "telephony") { input.connect(destination); return; }
  const high = ac.createBiquadFilter(); high.type = "highpass"; high.frequency.value = 300; high.Q.value = .707;
  const low = ac.createBiquadFilter(); low.type = "lowpass"; low.frequency.value = 3400; low.Q.value = .707;
  input.connect(high); high.connect(low); low.connect(destination);
}
function decode(buffer, codec = "pcm_s16le") {
  const view = new DataView(buffer), count = codec === "mulaw" ? view.byteLength : view.byteLength / 2, samples = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    if (codec === "mulaw") {
      const value = ~view.getUint8(i) & 255; let magnitude = ((value & 15) << 3) + 132;
      magnitude = (magnitude << ((value & 112) >> 4)) - 132; samples[i] = ((value & 128) ? -magnitude : magnitude) / 32768;
    } else samples[i] = view.getInt16(i * 2, true) / 32768;
  }
  return samples;
}
function encodeMulaw(buffer) {
  const view = new DataView(buffer), result = new Uint8Array(view.byteLength / 2);
  for (let i = 0; i < result.length; i++) {
    let sample = view.getInt16(i * 2, true); const sign = sample < 0 ? 128 : 0; sample = Math.min(32635, Math.abs(sample)) + 132;
    let exponent = 7; for (let mask = 16384; exponent > 0 && !(sample & mask); mask >>= 1) exponent--;
    result[i] = ~(sign | exponent << 4 | sample >> (exponent + 3) & 15) & 255;
  }
  return result;
}
function resample(input, from, to) {
  const output = new Float32Array(Math.max(1, Math.round(input.length * to / from)));
  for (let i = 0; i < output.length; i++) {
    const position = i * from / to, left = Math.floor(position), fraction = position - left;
    output[i] = input[Math.min(left, input.length - 1)] * (1 - fraction) + input[Math.min(left + 1, input.length - 1)] * fraction;
  }
  return output;
}
function drawSignal() {
  drawScheduled = false; const canvas = el("waveform"), rect = canvas.getBoundingClientRect(); if (!rect.width) return;
  const ratio = window.devicePixelRatio || 1; canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
  const draw = canvas.getContext("2d"); draw.scale(ratio, ratio);
  const width = rect.width, height = rect.height, floor = height - 6, scale = Math.max(.06, Number(el("speech").value) * 2.5);
  draw.strokeStyle = "#edf0f4"; draw.lineWidth = 1;
  for (const fraction of [.25, .5, .75, 1]) { draw.beginPath(); draw.moveTo(0, floor * fraction); draw.lineTo(width, floor * fraction); draw.stroke(); }
  const y = floor - Math.min(1, Number(el("speech").value) / scale) * (height - 15);
  draw.setLineDash([3, 4]); draw.strokeStyle = "#c39b64"; draw.beginPath(); draw.moveTo(0, y); draw.lineTo(width, y); draw.stroke(); draw.setLineDash([]);
  draw.strokeStyle = "#6281a7"; draw.lineWidth = 1.5; draw.beginPath(); const start = Math.max(0, 160 - levels.length);
  levels.forEach((level, i) => { const x = (start + i) / 159 * width, py = floor - Math.min(1, level / scale) * (height - 15); if (!i) draw.moveTo(x, py); else draw.lineTo(x, py); }); draw.stroke();
}
function scheduleDraw() { if (!drawScheduled) { drawScheduled = true; requestAnimationFrame(drawSignal); } }
function captureConstraints(strict = false) {
  const constraints = {channelCount: {ideal: 1}}, supported = navigator.mediaDevices?.getSupportedConstraints?.() || {};
  for (const name of ["echoCancellation", "noiseSuppression", "autoGainControl"]) if (supported[name]) constraints[name] = {[strict ? "exact" : "ideal"]: el(name).checked};
  if (el("micDevice").value) constraints.deviceId = {exact: el("micDevice").value}; return constraints;
}
function deviceInfo() {
  const settings = stream?.getAudioTracks()[0]?.getSettings();
  if (!settings) { el("deviceInfo").textContent = el("inputSource").value === "file" ? "Audio file source. Microphone DSP settings do not apply." : "Actual device settings appear after microphone permission."; return; }
  const flag = name => settings[name] == null ? "n/a" : settings[name] ? "on" : "off";
  el("deviceInfo").textContent = `${settings.sampleRate || "?"}Hz device · AEC ${flag("echoCancellation")} · NS ${flag("noiseSuppression")} · AGC ${flag("autoGainControl")}`;
}
async function applyCapture() {
  if (!context || !(ready || starting) || el("inputSource").value !== "microphone") return;
  const constraints = captureConstraints(true), version = runId, revision = ++captureRevision;
  el("captureState").textContent = "Applying microphone processing…";
  el("captureState").dataset.state = "pending";
  captureChanges = captureChanges.then(async () => {
    if (version !== runId || revision !== captureRevision) return;
    const track = stream?.getAudioTracks()[0];
    const previousSettings = track?.getSettings() || lastCaptureSettings;
    if (previousSettings) lastCaptureSettings = previousSettings;
    if (!constraints.deviceId && previousSettings?.deviceId) constraints.deviceId = {exact: previousSettings.deviceId};
    captureRefreshing = true;
    source?.disconnect(); source = null;
    stream?.getTracks().forEach(item => item.stop()); stream = null;
    monitorPlayer?.port.postMessage({type: "clear"});
    const connect = replacement => {
      if (version !== runId || revision !== captureRevision) { replacement.getTracks().forEach(item => item.stop()); return false; }
      stream = replacement; source = context.createMediaStreamSource(replacement); source.connect(inputGain);
      lastCaptureSettings = replacement.getAudioTracks()[0].getSettings(); return true;
    };
    try {
      if (!connect(await navigator.mediaDevices.getUserMedia({audio: constraints}))) return;
      deviceInfo(); const actual = stream.getAudioTracks()[0].getSettings();
      const unmatched = ["echoCancellation", "noiseSuppression", "autoGainControl"].filter(name => constraints[name] && actual[name] !== constraints[name].exact);
      el("captureState").textContent = unmatched.length ? `Device did not confirm: ${unmatched.join(", ")}. See actual settings above.` : `Applied live · microphone refreshed; session stays connected. ${monitoring ? "Live monitor continues." : "Choose Listen to my input to compare."}`;
      el("captureState").dataset.state = unmatched.length ? "pending" : "applied";
    } catch (error) {
      if (version !== runId || revision !== captureRevision) return;
      let restored = false;
      if (previousSettings) {
        const restore = {channelCount: {ideal: 1}};
        if (previousSettings.deviceId) restore.deviceId = {exact: previousSettings.deviceId};
        for (const name of ["echoCancellation", "noiseSuppression", "autoGainControl"]) if (typeof previousSettings[name] === "boolean") restore[name] = {exact: previousSettings[name]};
        try { restored = connect(await navigator.mediaDevices.getUserMedia({audio: restore})); } catch (_) {}
      }
      if (version !== runId || revision !== captureRevision) return;
      if (restored) {
        deviceInfo();
        for (const name of ["echoCancellation", "noiseSuppression", "autoGainControl"]) if (typeof lastCaptureSettings[name] === "boolean") el(name).checked = lastCaptureSettings[name];
      } else el("deviceInfo").textContent = "Microphone unavailable. Retry capture; the WebSocket session remains connected.";
      el("captureState").textContent = `Could not apply: ${error.message}.${restored ? " Previous processing restored." : " Use Retry microphone."}`;
      el("captureState").dataset.state = "error";
      log(`capture.error ${error.message}`);
    } finally {
      if (version === runId) {
        captureRefreshing = false;
        el("retryCapture").hidden = Boolean(stream?.getAudioTracks()[0]?.readyState === "live");
        if (ready && revision === captureRevision) monitorSettings();
      }
    }
  });
  await captureChanges;
}
async function microphones() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  try {
    const devices = await navigator.mediaDevices.enumerateDevices(), selected = el("micDevice").value;
    el("micDevice").replaceChildren(new Option("System default microphone", "")); let count = 0;
    for (const device of devices.filter(device => device.kind === "audioinput" && device.deviceId !== "default")) el("micDevice").add(new Option(device.label || `Microphone ${++count}`, device.deviceId));
    if ([...el("micDevice").options].some(option => option.value === selected)) el("micDevice").value = selected;
  } catch (error) { log(`device.list ${error.message}`); }
}
function setVad(vad) {
  for (const [id, key] of Object.entries(vadFields)) if (vad[key] != null) el(id).value = vad[key];
  if (vad.semantic_eot != null) el("semanticEot").checked = vad.semantic_eot;
  if (vad.filter_backchannels != null) el("backchannels").checked = vad.filter_backchannels; refreshVad();
}
function refreshVad() {
  for (const id of Object.keys(vadFields)) el(`${id}Value`).textContent = ["speech", "silence"].includes(id) ? Number(el(id).value).toFixed(3) : `${el(id).value}ms`;
  el("trailingSilence").disabled = !el("semanticEot").checked; el("vadHelp").textContent = vadDescriptions[el("vadPreset").value]; scheduleDraw();
}
function vadPayload() {
  const payload = {type: "vad.config", semantic_eot: el("semanticEot").checked, filter_backchannels: el("backchannels").checked};
  for (const [id, key] of Object.entries(vadFields)) payload[key] = Number(el(id).value); return payload;
}
function sendVad() {
  clearTimeout(vadTimer); vadTimer = null;
  if (!ready) { el("vadState").textContent = "Settings will apply when the session connects."; return; }
  if (Number(el("silence").value) >= Number(el("speech").value)) { el("vadState").textContent = "Silence threshold must stay below speech threshold."; el("vadState").dataset.state = "error"; return; }
  const payload = {...vadPayload(), request_id: ++vadRequestId};
  pendingVad = {id: payload.request_id, edit: vadEditVersion};
  el("vadState").textContent = "Applying live settings…"; el("vadState").dataset.state = "pending"; send(payload);
}
function chooseVad() {
  const choice = el("vadPreset").value;
  if (choice === "server" && serverVad) setVad(serverVad); else if (vadPresets[choice]) setVad(vadPresets[choice]);
  vadEditVersion++; refreshVad(); sendVad();
}
function editVad(id) {
  if (id === "speech" && Number(el("silence").value) >= Number(el("speech").value)) el("silence").value = Math.max(.001, Number(el("speech").value) - .001).toFixed(3);
  if (id === "silence" && Number(el("speech").value) <= Number(el("silence").value)) el("speech").value = Math.min(.15, Number(el("silence").value) + .001).toFixed(3);
  vadEditVersion++; el("vadPreset").value = "custom"; refreshVad();
  el("vadState").textContent = ready ? "Updating live…" : "Settings will apply when the session connects.";
  el("vadState").dataset.state = "pending";
  if (!vadTimer) vadTimer = setTimeout(sendVad, 80);
}
function displayVadStatus(message) {
  lastVadStatus = message;
  el("liveVad").textContent = message.speaking ? "VAD · speech detected" : "VAD · waiting for speech";
  el("liveVad").dataset.state = message.speaking ? "speech" : "silence";
  el("liveVad").title = `Server RMS ${Number(message.rms).toFixed(3)} · silence ${message.silence_ms}/${message.effective_silence_ms}ms · config r${message.revision}`;
}
function playbackSettings() {
  player?.port.postMessage({type: "settings", buffer_ms: Number(el("playbackBuffer").value), paused: playbackPaused});
  if (outputGain) outputGain.gain.setTargetAtTime(Number(el("volume").value) / 100, context.currentTime, .015);
  el("volumeValue").textContent = `${el("volume").value}%`; el("pausePlayback").textContent = playbackPaused ? "Resume agent" : "Pause agent";
}
async function loadAudioFile() {
  audioFileBuffer = null; updateControls(); const file = el("audioFile").files[0];
  if (!file) { el("fileInfo").textContent = "Choose an audio file up to 25 seconds / 10MB."; return; }
  if (file.size > 10 * 1024 * 1024) { notice("Choose an audio file smaller than 10MB."); return; }
  if (!context) { el("fileInfo").textContent = `${file.name} · connect the session to decode this file.`; return; }
  try {
    const buffer = await context.decodeAudioData(await file.arrayBuffer());
    if (buffer.duration > 25) throw new Error("Choose a clip no longer than 25 seconds to leave room for end-of-turn silence.");
    audioFileBuffer = buffer; el("fileInfo").textContent = `${file.name} · ${buffer.duration.toFixed(1)}s · ${buffer.sampleRate}Hz · ${buffer.numberOfChannels} channel(s)`; notice(""); updateControls();
  } catch (error) { notice(`Audio file could not be loaded: ${error.message}`); log(`audio.file.error ${error.message}`); }
}
function streamFile() {
  if (!ready || !audioFileBuffer || fileSource) return;
  fileSource = context.createBufferSource(); fileSource.buffer = audioFileBuffer; fileSource.connect(inputGain); el("streamFile").textContent = "Streaming caller audio…";
  fileSource.onended = () => { fileSource?.disconnect(); fileSource = null; el("streamFile").textContent = "Stream file as caller audio"; updateControls(); };
  fileSource.start(); updateControls(); log(`audio.file.start duration_seconds=${audioFileBuffer.duration.toFixed(2)}`);
}
function finishRecording() {
  recording = false;
  if (recordedFrames.length) {
    const bytes = new Uint8Array(recordingSamples * 2); let offset = 0;
    for (const frame of recordedFrames) { bytes.set(new Uint8Array(frame), offset); offset += frame.byteLength; }
    recordedClip = {samples: decode(bytes.buffer), rate: recordedRate}; el("clipInfo").textContent = `${(recordingSamples / recordedRate).toFixed(1)}s captured at ${recordedRate}Hz · saved snapshot. Use Listen to my input for live comparisons.`;
  }
  recordedFrames = []; el("recordClip").textContent = "Capture 5 seconds"; updateControls();
  monitorSettings();
}
function recordClip() {
  if (recording) { finishRecording(); return; } if (!ready) return;
  recording = true; recordedFrames = []; recordingSamples = 0; recordedRate = 16000;
  el("recordClip").textContent = "Finish capture"; el("clipInfo").textContent = "Capturing processed input…"; el("playClip").disabled = true;
  monitorSettings();
}
function playClip() {
  if (!context || !recordedClip) return; if (previewSource) { previewSource.stop(); return; }
  const buffer = context.createBuffer(1, recordedClip.samples.length, recordedClip.rate); buffer.copyToChannel(recordedClip.samples, 0);
  previewSource = context.createBufferSource(); previewSource.buffer = buffer; previewSource.connect(outputGain); localPreview = true; monitorPlayer?.port.postMessage({type: "clear"}); el("playClip").textContent = "Stop preview";
  previewSource.onended = () => { previewSource?.disconnect(); previewSource = null; localPreview = false; monitorSettings(); el("playClip").textContent = "Play capture"; };
  previewSource.start(); log("audio.capture.preview · caller audio sends silence during local preview");
}
function handlePCM(data) {
  const paused = inputPaused || localPreview || captureRefreshing, pcm = paused ? new ArrayBuffer(data.buffer.byteLength) : data.buffer;
  const level = paused ? 0 : data.rms; levels.push(level); if (levels.length > 160) levels.shift(); scheduleDraw(); el("rmsValue").textContent = `${level.toFixed(3)} RMS`;
  if (ready && ws?.readyState === WebSocket.OPEN) {
    if (ws.bufferedAmount > 64000) { notice("The audio connection is falling behind. Reconnect to keep real-time frame pacing."); stop(); return; }
    ws.send(encoding === "mulaw" ? encodeMulaw(pcm) : pcm);
  }
}
function monitorSettings() {
  send({type: "input.monitor", enabled: monitoring || recording});
  monitorPlayer?.port.postMessage({type: "clear"}); monitorSamples = 0;
  if (monitoring && !localPreview) {
    monitorPlayer?.port.postMessage({type: "generation", id: "input-monitor"});
    monitorPlayer?.port.postMessage({type: "settings", buffer_ms: 40, paused: false});
  }
  if (monitorGain) monitorGain.gain.setTargetAtTime(Number(el("monitorVolume").value) / 100, context.currentTime, .015);
  el("monitorInput").textContent = monitoring ? "Stop listening" : "Listen to my input";
  el("monitorInput").setAttribute("aria-pressed", String(monitoring));
}
function handleMonitor(data, meta) {
  if (recording) {
    recordedFrames.push(data.slice(0)); recordingSamples += data.byteLength / 2;
    el("clipInfo").textContent = `Capturing server input… ${(recordingSamples / recordedRate).toFixed(1)} / 5.0 seconds`;
    if (recordingSamples >= recordedRate * 5) finishRecording();
  }
  if (!monitoring || localPreview) return;
  const decoded = decode(data, meta.encoding);
  if (el("monitorSource").value === "vad" && !meta.vad_speaking) decoded.fill(0);
  const samples = resample(decoded, meta.sample_rate, context.sampleRate);
  const start = monitorSamples; monitorSamples += decoded.length;
  monitorPlayer.port.postMessage({type: "audio", generation: "input-monitor", samples, start_sample: start, end_sample: monitorSamples}, [samples.buffer]);
}
function configureResponse() {
  const enabled = el("responseMode").value === "conversation";
  el("responseHelp").textContent = enabled ? "Transcripts remain visible if reply generation fails." : "STT stays active; no LLM or TTS requests are made for your speech.";
  if (ready) send({type: "session.configure", response_enabled: enabled});
  updateControls();
}
function handlePlayer(data) {
  if (data.type === "playback.status") { el("queuedAudio").textContent = `${Math.round(data.buffered_ms)}ms`; el("underruns").textContent = data.underruns; return; }
  if (data.type === "overflow") {
    if (generation) blockedGenerations.add(generation); notice("Playback reached its 10-second queue limit. The response was cleared; resume playback before another turn."); send({type: "interrupt"}); return;
  }
  if (data.generation_id) send({type: data.type === "cleared" ? "playback.cleared" : "playback.ack", generation_id: data.generation_id, played_samples: data.played_samples});
}
function handleMessage(data) {
  if (typeof data !== "string") {
    const meta = metadata; metadata = null;
    if (meta?.type === "input.audio") { handleMonitor(data, meta); return; }
    if (!meta || meta.generation_id !== generation || blockedGenerations.has(meta.generation_id)) return;
    const assumed = el("rateSim").value === "auto" ? meta.sample_rate : Number(el("rateSim").value);
    const samples = resample(decode(data, meta.encoding), assumed, context.sampleRate);
    player.port.postMessage({type: "audio", generation: meta.generation_id, samples, start_sample: meta.start_sample, end_sample: meta.end_sample}, [samples.buffer]); return;
  }
  const message = JSON.parse(data); if (!["audio", "input.audio", "vad.status"].includes(message.type)) log(JSON.stringify(message));
  switch (message.type) {
    case "pipeline.stage": stage(message); break;
    case "session.ready": {
      ready = true; starting = false; encoding = message.encoding; connection("live"); serverVad = {...message.vad};
      if (el("vadPreset").value === "server") setVad(serverVad); else if (vadPresets[el("vadPreset").value]) setVad(vadPresets[el("vadPreset").value]);
      updateControls(); profileDisplay(); sendVad(); configureResponse(); monitorSettings();
      el("providerInfo").textContent = `${message.providers.stt} / ${message.providers.llm} / ${message.providers.tts}${message.providers.tts === "mock" ? " · mock audio uses synthetic tones" : ""}`;
      for (const name of ["stt", "llm", "tts"]) { el(`${name}Provider`).textContent = message.providers[name]; el(`${name}Provider`).title = message.models?.[name] || message.providers[name]; el(`${name}State`).textContent = message.models?.[name] ? message.models[name].split(/[\\/]/).at(-1) : "Ready"; }
      el("rateInfo").textContent = `${context.sampleRate}Hz audio device → ${message.sample_rate}Hz ${message.encoding} → 20ms packets`;
      activity(el("inputSource").value === "file" ? "Session ready · stream an audio file or send a text turn." : "Session ready · speak into the microphone.", "idle"); break;
    }
    case "audio.start": generation = message.generation_id; metadata = null; player.port.postMessage({type: "generation", id: generation}); row("Agent", generation); break;
    case "audio": case "input.audio": metadata = message; break;
    case "audio.end": player.port.postMessage({type: "end", generation: message.generation_id}); break;
    case "assistant.token": if (!blockedGenerations.has(message.generation_id)) row("Agent", message.generation_id).text.textContent += message.text; break;
    case "transcript": {
      const item = row("Caller", `caller-${runId}-${message.utterance_id || currentUtterance}`);
      item.container.dataset.partial = !message.final; item.text.textContent = message.text;
      el("latestTranscript").textContent = message.text; el("recognitionState").textContent = message.final ? "Recognized · saved in conversation below" : "Partial · recognizing";
      el("recognitionReadout").dataset.state = message.final ? "final" : "partial";
      el("transcript").scrollTop = el("transcript").scrollHeight; break;
    }
    case "turn.transcribed": activity("Transcription saved · listening (LLM / TTS off)", "idle"); break;
    case "session.config.ack":
      el("responseHelp").textContent = message.response_enabled ? "Conversation enabled. Transcripts remain visible if reply generation fails." : "Transcription only is active. No LLM or TTS requests for your speech.";
      if (!message.response_enabled) for (const name of ["llm", "tts"]) { el(`stage-${name}`).dataset.state = "disabled"; el(`${name}State`).textContent = "Off · transcription only"; }
      break;
    case "barge_in_clear": blockedGenerations.add(message.generation_id); player.port.postMessage({type: "clear"}); generation = metadata = null; el("cancel").textContent = metric(message.barge_in_cancel_latency_ms); syncTranscript(message); activity("Agent interrupted · playback cleared", "idle"); break;
    case "CLEAR_BUFFER": if (generation === message.generation_id) { player.port.postMessage({type: "clear"}); generation = metadata = null; } break;
    case "context.sync": syncTranscript(message); break;
    case "generation.cancelled":
      if (!generation || generation === message.generation_id) {
        for (const name of ["llm", "tts"]) {
          el(`stage-${name}`).dataset.state = "cancelled";
          el(`${name}State`).textContent = "Interrupted";
        }
      }
      break;
    case "turn.window": el("eot").textContent = `${message.silence_ms}ms ${message.reason}`; break;
    case "backchannel.ignored": activity("Brief speech or passive backchannel ignored · agent continues", "idle"); break;
    case "metrics": if (message.ttfa_ms != null) el("ttfa").textContent = metric(message.ttfa_ms); if (message.barge_in_cancel_latency_ms != null) el("cancel").textContent = metric(message.barge_in_cancel_latency_ms); break;
    case "vad.config.ack": {
      if (message.request_id == null) {
        el("vadState").textContent = "Server is running older live-tuning code. Restart Python once to load the update.";
        el("vadState").dataset.state = "error";
        notice("The page has newer live-tuning controls than the running server. Restart the Python server once, then reload this page. Further tuning will use the same session.");
        break;
      }
      if (!pendingVad || message.request_id !== pendingVad.id || pendingVad.edit !== vadEditVersion) break;
      setVad(message.vad);
      el("vadState").textContent = `Applied live · r${message.vad.revision} · ${message.vad.min_speech_ms}ms speech / ${message.vad.min_silence_ms}ms silence`;
      el("vadState").dataset.state = "applied";
      el("appliedVad").textContent = `Server r${message.vad.revision} · speech ≥${Number(message.vad.speech_threshold).toFixed(3)} · silence ≤${Number(message.vad.silence_threshold).toFixed(3)}`;
      break;
    }
    case "vad.status": displayVadStatus(message); break;
    case "error":
      activity(`${message.code} · see protocol events for details`, "error"); notice(message.message?.slice(0, 300) || message.code);
      if (["stt", "llm", "tts"].includes(message.stage)) { el(`stage-${message.stage}`).dataset.state = "failed"; el(`${message.stage}State`).textContent = "Provider failed"; }
      if (message.stage === "llm" || message.stage === "tts") {
        const item = row("Agent", message.generation_id);
        const note = document.createElement("span"); note.className = "response-error";
        note.textContent = `${message.stage.toUpperCase()} unavailable. Your transcription is preserved; you can keep speaking or choose Transcription only.`;
        item.text.append(note);
        el("recognitionState").textContent = "Transcription preserved · reply provider unavailable";
        if (message.stage === "llm") { el("stage-tts").dataset.state = "blocked"; el("ttsState").textContent = "Waiting for LLM"; }
      }
      if (message.code === "INVALID_VAD_CONFIG" && (!message.request_id || message.request_id === pendingVad?.id)) { el("vadState").textContent = message.message; el("vadState").dataset.state = "error"; } break;
  }
}
async function start() {
  if (starting || ready) return;
  const version = ++runId; starting = true; closing = false; currentUtterance = 0; serverVad = null;
  pendingVad = lastVadStatus = null; vadRequestId = vadEditVersion = 0;
  captureChanges = Promise.resolve(); captureRefreshing = false; lastCaptureSettings = null; el("retryCapture").hidden = true;
  blockedGenerations.clear(); notice(""); updateControls(); connection("connecting"); activity("Preparing audio and WebSocket connection…");
  try {
    if (!navigator.mediaDevices?.getUserMedia && el("inputSource").value === "microphone") throw new Error("Microphone access requires localhost or HTTPS in a supported browser.");
    const ac = new AudioContext({latencyHint: "interactive"}); context = ac;
    await ac.resume(); if (version !== runId) return;
    await ac.audioWorklet.addModule("/static/audio-worklet.js"); if (version !== runId) return;
    capture = new AudioWorkletNode(ac, "pcm-capture", {processorOptions: {rate: wireRate()}, channelCount: 1, channelCountMode: "explicit"}); player = new AudioWorkletNode(ac, "pcm-player", {outputChannelCount: [1]});
    monitorPlayer = new AudioWorkletNode(ac, "pcm-player", {outputChannelCount: [1]}); monitorGain = ac.createGain(); monitorGain.gain.value = Number(el("monitorVolume").value) / 100;
    monitorPlayer.connect(monitorGain); monitorGain.connect(ac.destination);
    monitorPlayer.port.onmessage = ({data}) => {
      if (data.type === "overflow") { monitoring = false; monitorSettings(); el("monitorInfo").textContent = "Monitor stopped because playback fell behind. Start listening again to resume."; }
    };
    inputGain = ac.createGain(); inputGain.gain.value = Number(el("inputGain").value); bandpass(inputGain, capture, ac);
    outputGain = ac.createGain(); outputGain.gain.value = Number(el("volume").value) / 100; outputGain.connect(ac.destination); bandpass(player, outputGain, ac);
    capture.connect(ac.destination); capture.port.onmessage = ({data}) => handlePCM(data); player.port.onmessage = ({data}) => handlePlayer(data); playbackSettings();
    if (el("inputSource").value === "microphone") {
      const incoming = await navigator.mediaDevices.getUserMedia({audio: captureConstraints()});
      if (version !== runId) { incoming.getTracks().forEach(track => track.stop()); return; }
      stream = incoming; source = ac.createMediaStreamSource(stream); source.connect(inputGain); deviceInfo(); await microphones();
      lastCaptureSettings = incoming.getAudioTracks()[0].getSettings();
    } else { deviceInfo(); await loadAudioFile(); }
    if (version !== runId) return;
    const url = new URL("/ws/voice", location.href); url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("mode", el("mode").value); url.searchParams.set("codec", el("codec").value); if (el("token").value) url.searchParams.set("token", el("token").value);
    const socket = new WebSocket(url); ws = socket; socket.binaryType = "arraybuffer";
    socket.onopen = () => { if (version === runId) send({type: "session.start", session_id: crypto.randomUUID()}); };
    socket.onerror = () => { if (version === runId) notice("WebSocket connection failed. Check the server’s allowed origin and session token."); };
    socket.onclose = event => { if (version !== runId) return; log(`socket.closed code=${event.code}`); if (!closing) stop(); };
    socket.onmessage = ({data}) => { if (version === runId) handleMessage(data); };
  } catch (error) {
    if (version !== runId) return; log(`session.error ${error.message}`); await stop(); notice(error.message); activity(`Could not start: ${error.message}`, "error");
  }
}
async function stop() {
  if (closing) return; closing = true; runId++; ready = starting = false; clearTimeout(vadTimer);
  if (recording) finishRecording(); player?.port.postMessage({type: "clear"}); send({type: "session.end"}); ws?.close(); stream?.getTracks().forEach(track => track.stop());
  try { fileSource?.stop(); previewSource?.stop(); } catch (_) {}
  source?.disconnect(); capture?.disconnect(); player?.disconnect(); monitorPlayer?.disconnect(); monitorGain?.disconnect(); const ac = context;
  ws = stream = context = source = capture = player = inputGain = outputGain = fileSource = previewSource = generation = metadata = null;
  monitorPlayer = monitorGain = null; monitoring = false; monitorSamples = 0;
  captureRefreshing = false; lastCaptureSettings = null; el("retryCapture").hidden = true;
  el("monitorInput").textContent = "Listen to my input"; el("monitorInput").setAttribute("aria-pressed", "false");
  inputPaused = playbackPaused = localPreview = false;
  pendingVad = lastVadStatus = null; el("liveVad").textContent = "VAD · offline"; el("liveVad").dataset.state = "idle";
  el("appliedVad").textContent = "Server settings appear after connection.";
  el("pauseInput").textContent = "Pause input"; el("pausePlayback").textContent = "Pause agent"; el("playClip").textContent = "Play capture"; el("streamFile").textContent = "Stream file as caller audio";
  connection("idle"); updateControls(); activity("Session stopped. Your tuning and captured audio remain in this page.", "idle");
  if (ac && ac.state !== "closed") await ac.close();
}
for (const id of Object.keys(vadFields)) {
  el(id).oninput = () => editVad(id); el(id).onchange = sendVad;
}
for (const id of ["semanticEot", "backchannels"]) el(id).onchange = () => { vadEditVersion++; el("vadPreset").value = "custom"; refreshVad(); sendVad(); };
el("vadPreset").onchange = chooseVad;
el("resetVad").onclick = () => { vadEditVersion++; el("vadPreset").value = "server"; if (serverVad) setVad(serverVad); else setVad(vadPresets[el("mode").value === "telephony" ? "phone" : "balanced"]); refreshVad(); sendVad(); };
el("capturePreset").onchange = () => {
  const preset = capturePresets[el("capturePreset").value]; if (preset) { for (const id of ["echoCancellation", "noiseSuppression", "autoGainControl"]) el(id).checked = preset[id]; el("captureHelp").textContent = preset.help; applyCapture(); }
};
for (const id of ["echoCancellation", "noiseSuppression", "autoGainControl"]) el(id).onchange = () => { el("capturePreset").value = "custom"; el("captureHelp").textContent = "Browser support varies. Actual applied settings are shown below."; applyCapture(); };
el("retryCapture").onclick = applyCapture;
el("inputGain").oninput = () => { el("inputGainValue").textContent = `${Number(el("inputGain").value).toFixed(2)}×`; if (inputGain) inputGain.gain.setTargetAtTime(Number(el("inputGain").value), context.currentTime, .015); };
el("playbackBuffer").onchange = playbackSettings; el("volume").oninput = playbackSettings;
el("start").onclick = start; el("stop").onclick = stop;
el("mode").onchange = () => { profileDisplay(); if (el("vadPreset").value === "server") setVad(vadPresets[el("mode").value === "telephony" ? "phone" : "balanced"]); };
el("codec").onchange = profileDisplay; el("inputSource").onchange = () => { updateControls(); deviceInfo(); };
el("audioFile").onchange = loadAudioFile; el("streamFile").onclick = streamFile; el("recordClip").onclick = recordClip; el("playClip").onclick = playClip;
el("responseMode").onchange = configureResponse;
el("monitorInput").onclick = () => { monitoring = !monitoring; monitorSettings(); };
el("monitorSource").onchange = monitorSettings;
el("monitorVolume").oninput = () => { el("monitorVolumeValue").textContent = `${el("monitorVolume").value}%`; if (monitorGain) monitorGain.gain.setTargetAtTime(Number(el("monitorVolume").value) / 100, context.currentTime, .015); };
el("pauseInput").onclick = () => { inputPaused = !inputPaused; el("pauseInput").textContent = inputPaused ? "Resume input" : "Pause input"; activity(inputPaused ? "Caller input paused · streaming silence" : "Caller input resumed", inputPaused ? "idle" : "active"); };
el("pausePlayback").onclick = () => { playbackPaused = !playbackPaused; playbackSettings(); };
el("flushPlayback").onclick = () => { if (generation) blockedGenerations.add(generation); player?.port.postMessage({type: "clear"}); metadata = null; send({type: "interrupt"}); log("playback.flush requested"); };
el("clearTranscript").onclick = () => { rows.clear(); el("transcript").replaceChildren(); };
el("clearLog").onclick = () => { logLines = []; el("log").textContent = "Awaiting session events."; };
el("copyLog").onclick = async () => { try { await navigator.clipboard.writeText(logLines.join("\n")); el("copyLog").textContent = "Copied"; setTimeout(() => { el("copyLog").textContent = "Copy"; }, 1500); } catch (_) { notice("Copy is unavailable here. Select the protocol text to copy it manually."); } };
el("textForm").onsubmit = event => { event.preventDefault(); const text = el("text").value.trim(); if (text && ready) { notice(""); send({type: "user.text", text}); row("Caller · text").text.textContent = text; el("text").value = ""; } };
el("benchmark").onclick = async () => {
  el("benchmark").disabled = true; el("benchmarkStatus").textContent = "Running 30 concurrent calls…"; el("results").replaceChildren();
  try {
    const headers = el("token").value ? {Authorization: `Bearer ${el("token").value}`} : {};
    const response = await fetch(`/api/benchmark?concurrency=30&mode=${el("mode").value}`, {headers}); const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    const labels = {ttfa_ms: "Time to first audio", end_to_end_turn_latency_ms: "End-to-end turn", barge_in_cancel_latency_ms: "Barge-in cancellation"};
    for (const [key, stats] of Object.entries(result.metrics)) {
      const tr = document.createElement("tr");
      for (const value of [labels[key], ...["p50", "p95", "p99", "max"].map(name => stats[name] == null ? "n/a" : Number(stats[name]).toFixed(2))]) { const td = document.createElement("td"); td.textContent = value; tr.append(td); } el("results").append(tr);
    }
    el("benchmarkStatus").textContent = `${result.successful}/30 calls succeeded · ${result.mode} · ${(result.duration_ms / 1000).toFixed(1)}s. Client timings include transport. Voiced tones exercise mock recognition, not real STT accuracy.`;
    for (const call of result.calls) if (!call.ok) log(`benchmark.call ${call.call_id} ${call.error}`);
  } catch (error) { el("benchmarkStatus").textContent = `Benchmark failed: ${error.message}`; }
  finally { el("benchmark").disabled = false; }
};
window.addEventListener("resize", scheduleDraw); window.addEventListener("pagehide", () => { stream?.getTracks().forEach(track => track.stop()); ws?.close(); });
navigator.mediaDevices?.addEventListener?.("devicechange", microphones);
profileDisplay(); refreshVad(); updateControls(); scheduleDraw();

(() => {
  const descriptions = {
    mode: "Wideband preserves more frequencies at 16kHz. Phone simulation sends 8kHz audio through a 300–3400Hz bandpass. Choose before connecting; both use WebSockets.",
    codec: "PCM preserves each 16-bit sample. G.711 μ-law compresses telephony samples to 8 bits, changing quiet-signal detail. This affects the audio sent to STT, not its model.",
    responseMode: "Transcription only runs recognition without LLM or TTS requests. Choose it to test local Whisper when Gemini has no quota. Conversation mode adds generated replies.",
    capturePreset: "Presets request combinations of browser processing. Raw requests echo cancellation, noise suppression, and automatic gain off. The device readout shows what was actually applied.",
    echoCancellation: "Reduces loudspeaker audio picked up by the microphone. Toggle while monitoring to compare; with headphones or no speaker leakage the difference may be small. It does not remove general room noise.",
    noiseSuppression: "Reduces background noise such as a fan or hum. Compare while that noise is present; quiet speech in a quiet room may sound similar. Strong processing can also alter soft speech.",
    autoGainControl: "Automatically changes microphone loudness. It can make quiet speech easier to hear, but it also changes RMS levels and can make noise more noticeable. Disable it for a steadier VAD comparison.",
    inputGain: "Multiplies the signal after browser processing. More gain raises speech and noise together, which can trigger VAD sooner. Excess gain clips the waveform; this is separate from automatic gain.",
    speech: "RMS energy needed to begin detecting speech. Raise it to reject noise; lower it for quiet voices. Lowering it below the current silence threshold also lowers that threshold to keep the pair valid. Changes apply while streaming.",
    silence: "RMS level below which a speaking turn counts as silent. Raising it above the speech threshold also raises that threshold to keep the pair valid. This controls the end of an active turn; the speech threshold controls its start.",
    minSpeech: "How long sound must stay above the speech threshold before an utterance starts. Longer durations reject clicks and coughs; shorter durations react faster. This differs from the barge-in duration.",
    minSilence: "How long a pause ends a complete utterance. Longer waits allow thinking pauses but delay replies. Semantic end of turn can extend this wait for an unfinished clause.",
    semanticEot: "Uses transcript endings to choose a longer silence window for phrases such as ‘because’ or ‘and’. Turn it off for a fixed silence window. Batch Whisper can only supply this hint after recognition finishes.",
    trailingSilence: "The extended pause allowed for an unfinished clause. It never shortens the base silence duration. Increasing it reduces interruptions mid-thought at the cost of slower replies.",
    bargeSpeech: "Voiced duration required before a user interruption can cancel agent playback. The transcript must also pass the backchannel guard. The minimum is 320ms; recognition can add further delay.",
    backchannels: "Ignores passive single-word responses such as ‘yeah’ or ‘okay’ during agent playback. Disable it if those words should be treated as intentional interruptions; the minimum voiced duration still applies.",
    playbackBuffer: "Collects agent audio before playing it. A larger prebuffer can smooth uneven delivery, but you hear the first audio later. Server TTFA does not include this added speaker delay.",
    rateSim: "Deliberately interprets playback at a different sample rate, changing pitch and speed. Leave Auto selected for normal sound. It changes neither microphone quality nor the rate sent to Whisper.",
    monitorSource: "Processed input lets you hear noise and echo processing. VAD speech gate additionally mutes the monitor when server VAD says there is no speech, so thresholds and durations become audible. This preview does not change STT audio.",
  };
  const tooltip = document.createElement("div"); tooltip.id = "settingTooltip"; tooltip.role = "tooltip"; tooltip.hidden = true; document.body.append(tooltip);
  let anchor = null, timer;
  function hide() { clearTimeout(timer); if (anchor) anchor.removeAttribute("aria-describedby"); anchor = null; tooltip.hidden = true; }
  function position() {
    if (!anchor) return;
    const box = anchor.getBoundingClientRect(), width = tooltip.offsetWidth, height = tooltip.offsetHeight;
    tooltip.style.left = `${Math.max(10, Math.min(box.left, innerWidth - width - 10))}px`;
    const below = box.bottom + 8;
    tooltip.style.top = `${Math.max(10, below + height <= innerHeight - 10 ? below : box.top - height - 8)}px`;
  }
  function show(button, text) {
    hide(); anchor = button; tooltip.textContent = text; tooltip.hidden = false;
    button.setAttribute("aria-describedby", tooltip.id); position();
  }
  for (const [id, text] of Object.entries(descriptions)) {
    const label = document.querySelector(`label[for="${id}"]`); if (!label) continue;
    const wrapper = document.createElement("span"); wrapper.className = label.classList.contains("field-label") ? "help-label field-heading" : "help-label";
    label.replaceWith(wrapper); const button = document.createElement("button"); button.type = "button"; button.className = "setting-help"; button.textContent = "?";
    button.setAttribute("aria-label", `Help: ${label.firstChild.textContent.trim()}`); wrapper.append(label, button);
    button.addEventListener("pointerenter", () => show(button, text)); button.addEventListener("focus", () => show(button, text));
    button.addEventListener("click", () => show(button, text));
    button.addEventListener("pointerleave", () => { if (document.activeElement !== button) timer = setTimeout(hide, 180); }); button.addEventListener("blur", hide);
  }
  tooltip.addEventListener("pointerenter", () => clearTimeout(timer)); tooltip.addEventListener("pointerleave", hide);
  document.addEventListener("keydown", event => { if (event.key === "Escape") hide(); });
  document.addEventListener("pointerdown", event => { if (anchor && !anchor.contains(event.target) && !tooltip.contains(event.target)) hide(); });
  window.addEventListener("scroll", hide, true); window.addEventListener("resize", hide);
})();
