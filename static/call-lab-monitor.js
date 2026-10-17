'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let selected = '', active = false, socket, context, player, gain, connecting;
  let listening = false, recording = false, frames = [], count = 0, clip, preview;
  let sampleIndex = 0, revision = 0;

  function status(text) { $('labMonitorStatus').textContent = text; }
  function controls() {
    $('labListen').disabled = !active;
    $('labCapture').disabled = !active && !recording;
    $('labListen').textContent = listening ? 'Stop listening' : 'Listen to caller input';
    $('labListen').setAttribute('aria-pressed', String(listening));
    $('labCapture').textContent = recording ? 'Finish capture' : 'Capture 5 seconds';
    $('labPlayCapture').disabled = !clip;
    $('labPlayCapture').textContent = preview ? 'Stop capture playback' : 'Play capture';
  }
  function clearPlayer() {
    player?.port.postMessage({type: 'generation', id: 'call-input'});
    sampleIndex = 0;
  }
  function finishCapture() {
    if (!recording) return;
    recording = false;
    if (count) {
      clip = new Int16Array(count);
      let offset = 0;
      for (const frame of frames) { clip.set(frame, offset); offset += frame.length; }
      status(`Captured ${(count / 16000).toFixed(1)} seconds of received STT input. Stored in this page only.`);
    } else status('No input audio received yet.');
    frames = []; count = 0; controls(); disconnectIfIdle();
  }
  function disconnectIfIdle() {
    if (!listening && !recording) { const current = socket; socket = null; current?.close(); }
  }
  function stop() {
    revision++;
    listening = false; finishCapture();
    const current = socket; socket = null; current?.close();
    clearPlayer(); controls();
  }
  async function audio() {
    if (!context || context.state === 'closed') {
      context = new AudioContext();
      await context.audioWorklet.addModule('/static/audio-worklet.js');
      player = new AudioWorkletNode(context, 'pcm-player', {outputChannelCount: [1]});
      gain = context.createGain(); gain.gain.value = Number($('labMonitorVolume').value) / 100;
      player.connect(gain); gain.connect(context.destination); clearPlayer();
      player.port.onmessage = ({data}) => {
        if (data.type === 'overflow') { stop(); status('Preview fell behind. Start listening again to resume live audio.'); }
      };
    }
    await context.resume();
    player.port.postMessage({type: 'settings', buffer_ms: Number($('labMonitorBuffer').value), paused: false});
  }
  function receive(data) {
    if (!(data instanceof ArrayBuffer) || data.byteLength < 3 || data.byteLength % 2 !== 1) return;
    const view = new DataView(data), speaking = view.getUint8(0) !== 0;
    const pcm = new Int16Array((data.byteLength - 1) / 2);
    for (let i = 0; i < pcm.length; i++) pcm[i] = view.getInt16(1 + i * 2, true);
    if (recording) {
      const part = pcm.slice(0, Math.min(pcm.length, 80000 - count));
      frames.push(part); count += part.length;
      status(`Capturing received STT input… ${(count / 16000).toFixed(1)} / 5.0 seconds`);
      if (count >= 80000) finishCapture();
    }
    if (!listening || preview) return;
    const length = Math.round(pcm.length * context.sampleRate / 16000), samples = new Float32Array(length);
    if ($('labMonitorSource').value !== 'vad' || speaking) {
      for (let i = 0; i < length; i++) {
        const position = i * 16000 / context.sampleRate, index = Math.floor(position), fraction = position - index;
        samples[i] = ((pcm[index] || 0) * (1 - fraction) + (pcm[Math.min(index + 1, pcm.length - 1)] || 0) * fraction) / 32768;
      }
    }
    const start = sampleIndex; sampleIndex += pcm.length;
    player.port.postMessage({type: 'audio', generation: 'call-input', samples, start_sample: start, end_sample: sampleIndex}, [samples.buffer]);
  }
  async function connect() {
    if (socket?.readyState === WebSocket.OPEN) return;
    if (connecting) return connecting;
    const callId = selected, version = revision;
    connecting = (async () => {
      await audio();
      if (!active || selected !== callId || revision !== version) throw new Error('Select an active call.');
      await new Promise((resolve, reject) => {
        const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/call-lab/monitor`);
        socket = ws; ws.binaryType = 'arraybuffer';
        let ready = false;
        const timer = setTimeout(() => { ws.close(); reject(new Error('Input preview connection timed out.')); }, 6000);
        ws.onopen = () => ws.send(JSON.stringify({token: $('labToken').value.trim(), call_id: callId}));
        ws.onmessage = event => {
          if (socket !== ws) return;
          if (typeof event.data === 'string') {
            const message = JSON.parse(event.data);
            if (message.type === 'monitor.ready') { ready = true; clearTimeout(timer); resolve(); }
          } else receive(event.data);
        };
        ws.onerror = () => { clearTimeout(timer); reject(new Error('Could not connect the input preview. Check the dashboard token.')); };
        ws.onclose = () => {
          clearTimeout(timer);
          if (!ready) reject(new Error('Input preview unavailable. Reconnect the dashboard and select an active call.'));
          if (socket === ws) { socket = null; listening = false; finishCapture(); clearPlayer(); controls(); status('Input preview disconnected.'); }
        };
      });
    })();
    try { await connecting; } finally { connecting = null; }
  }
  $('labListen').onclick = async () => {
    if (listening) { listening = false; clearPlayer(); disconnectIfIdle(); controls(); status('Listening stopped. The call continues.'); return; }
    try { await connect(); listening = true; clearPlayer(); controls(); status('Listening to received STT input. Use headphones.'); }
    catch (error) { status(error.message); }
  };
  $('labCapture').onclick = async () => {
    if (recording) { finishCapture(); return; }
    try { await connect(); frames = []; count = 0; recording = true; controls(); status('Capturing up to 5 seconds of received STT input…'); }
    catch (error) { status(error.message); }
  };
  $('labPlayCapture').onclick = async () => {
    if (preview) { preview.stop(); return; }
    if (!clip) return;
    try {
      await audio(); clearPlayer();
      const buffer = context.createBuffer(1, clip.length, 16000), samples = buffer.getChannelData(0);
      for (let i = 0; i < clip.length; i++) samples[i] = clip[i] / 32768;
      const source = context.createBufferSource(); source.buffer = buffer; source.connect(gain); preview = source;
      source.onended = () => { source.disconnect(); if (preview === source) preview = null; clearPlayer(); controls(); };
      source.start(); controls();
    } catch (error) { status(error.message); }
  };
  $('labMonitorSource').onchange = clearPlayer;
  $('labMonitorBuffer').onchange = () => {
    if ($('labMonitorBuffer').checkValidity()) player?.port.postMessage({type: 'settings', buffer_ms: Number($('labMonitorBuffer').value)});
  };
  $('labMonitorVolume').oninput = () => {
    $('labMonitorVolumeValue').textContent = `${$('labMonitorVolume').value}%`;
    gain?.gain.setTargetAtTime(Number($('labMonitorVolume').value) / 100, context.currentTime, .015);
  };
  window.callLabMonitor = {
    update(callId, live) {
      if (callId !== selected || (!live && active)) { stop(); preview?.stop(); }
      selected = callId; active = live; controls();
    },
  };
  window.addEventListener('pagehide', () => { stop(); preview?.stop(); context?.close(); });
})();
