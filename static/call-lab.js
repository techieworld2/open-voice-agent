'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const fields = {
    labSpeech: 'speech_threshold', labSilence: 'silence_threshold',
    labMinSpeech: 'min_speech_ms', labMinSilence: 'min_silence_ms',
    labTrailing: 'trailing_silence_ms', labBarge: 'barge_in_min_speech_ms',
    labSemantic: 'semantic_eot', labBackchannels: 'filter_backchannels',
  };
  let token = '', selected = '', calls = [], editing = false, saving = false, pending = false;
  let networkBusy = false, lastSnapshot = null, stopped = false;
  let previousActive = new Set();

  function notice(message = '') {
    $('labNotice').textContent = message;
    $('labNotice').hidden = !message;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options, cache: 'no-store', signal: AbortSignal.timeout(5000),
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...options.headers },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`);
    return data;
  }

  function metric(id, value) {
    $(id).replaceChildren(document.createTextNode(Number.isFinite(value) ? value.toFixed(1) + ' ' : '— '));
    const unit = document.createElement('em'); unit.textContent = 'ms'; $(id).append(unit);
  }

  function renderTranscript(rows) {
    const panel = $('labTranscript');
    const nearBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 50;
    if (!rows.length) {
      panel.replaceChildren();
      const empty = document.createElement('div'); empty.className = 'empty-state';
      const title = document.createElement('h3'); title.textContent = 'Waiting for speech';
      const text = document.createElement('p'); text.textContent = 'Speak through your softphone to see the transcript.';
      empty.append(title, text); panel.append(empty); return;
    }
    const fragment = document.createDocumentFragment();
    for (const row of rows) {
      const item = document.createElement('div'); item.className = 'transcript-row'; item.dataset.role = row.role;
      item.dataset.partial = String(row.role === 'user' && !row.final);
      const meta = document.createElement('div'); meta.className = 'message-meta';
      const label = document.createElement('strong'); label.textContent = row.role === 'agent' ? 'Agent' : 'Caller'; meta.append(label);
      const body = document.createElement('div'); body.className = 'message-body'; body.textContent = row.text;
      if (row.interrupted) {
        const discarded = document.createElement('span'); discarded.className = 'discarded';
        discarded.textContent = (row.text ? ' ' : '') + (row.discarded_text || ''); body.append(discarded);
        const note = document.createElement('span'); note.className = 'interrupted'; note.textContent = '[Interrupted — Truncated from LLM History]'; body.append(note);
      }
      item.append(meta, body); fragment.append(item);
    }
    panel.replaceChildren(fragment);
    if (nearBottom) panel.scrollTop = panel.scrollHeight;
  }

  function render(data) {
    lastSnapshot = data; calls = data.calls;
    $('sipHost').textContent = data.sip_host || 'Your computer’s LAN IP';
    $('sipPort').textContent = data.sip_port;
    $('labSetupState').textContent = !data.enabled ? 'Lab is disabled' : data.configured ? 'Bridge enabled' : 'Bridge token missing';
    $('labSetupState').dataset.state = data.enabled && data.configured ? 'ready' : 'error';
    $('labConnection').dataset.state = data.enabled && data.configured ? 'live' : '';
    const active = calls.filter(call => call.state === 'active');
    $('labConnectionText').textContent = !data.enabled ? 'Call lab disabled' : active.length ? `${active.length} active call${active.length === 1 ? '' : 's'}` : 'Waiting for a call';
    const gateway = data.gateway || {};
    const fs = gateway.freeswitch || {};
    $('gatewayStatus').textContent = !gateway.running ? gateway.detail : !fs.connected ? fs.detail : `${fs.version} · ${fs.detail}`;
    if (data.enabled && !active.length) $('labConnectionText').textContent = !gateway.running ? 'Gateway unavailable' : fs.connected && fs.profiles_ready ? 'Ready for a call' : 'Waiting for FreeSWITCH';
    $('labConnection').dataset.state = active.length || (gateway.running && fs.connected && fs.profiles_ready) ? 'live' : '';
    $('providerSummary').textContent = Object.entries(data.providers).map(([kind, name]) => `${kind.toUpperCase()}: ${name}`).join(' · ');
    for (const kind of ['stt', 'llm', 'tts']) $(`labProvider${kind[0].toUpperCase() + kind.slice(1)}`).textContent = data.providers[kind];
    const ids = new Set(calls.map(call => call.id));
    const newCall = active.find(call => !previousActive.has(call.id));
    if (!ids.has(selected) || (newCall && !active.some(call => call.id === selected))) selected = newCall?.id || active[0]?.id || calls[0]?.id || '';
    previousActive = new Set(active.map(call => call.id));
    const picker = $('callSelect');
    picker.replaceChildren(...(calls.length ? calls.map(call => {
      const option = document.createElement('option'); option.value = call.id;
      option.textContent = `${new Date(call.started_at * 1000).toLocaleTimeString()} · ${call.state} · ${call.id.slice(0, 6)}`;
      return option;
    }) : [new Option('No calls yet', '')]));
    picker.value = selected;
    const call = calls.find(item => item.id === selected), live = call?.state === 'active';
    window.callLabMonitor?.update(selected, Boolean(live));
    $('hangup').disabled = !live;
    for (const id of [...Object.keys(fields), 'callBehavior']) $(id).disabled = !live;
    if (!call) return;
    $('playbackBasis').textContent = call.playback_basis || data.playback_precision;
    metric('labTtfa', call.metrics.ttfa_ms); metric('labCancel', call.metrics.barge_in_cancel_latency_ms);
    $('flushDetail').textContent = 'Agent stream cancellation';
    if (Number.isFinite(call.metrics.gateway_queue_clear_ms)) $('flushDetail').textContent = `Local RTP queue cleared in ${call.metrics.gateway_queue_clear_ms.toFixed(2)}ms`;
    $('labEot').textContent = `${call.window.silence_ms}ms ${call.window.reason}`;
    $('callFrames').textContent = `${call.inbound_frames} received / ${call.outbound_frames} sent frames`;
    if (!editing && !saving && !pending && live) {
      for (const [id, key] of Object.entries(fields)) {
        if ($(id).type === 'checkbox') $(id).checked = Boolean(call.vad[key]);
        else $(id).value = call.vad[key];
      }
      $('callBehavior').value = call.response_enabled ? 'conversation' : 'transcription';
      $('callSettingsStatus').textContent = `Applied · revision ${call.vad.revision}`;
    }
    for (const kind of ['stt', 'llm', 'tts']) {
      const stage = $(`labStage${kind[0].toUpperCase() + kind.slice(1)}`), state = call.stages[kind]?.status || 'idle';
      stage.dataset.state = state; stage.querySelector('.stage-state').textContent = state;
    }
    const error = [...call.events].reverse().find(event => event.type === 'error');
    $('labActivity').textContent = !live ? 'Call ended.' : error ? error.message : `Call active · ${call.channel}`;
    $('labActivityDot').dataset.state = !live ? '' : error ? 'error' : 'active';
    renderTranscript(call.conversation);
    const log = $('labLog'), atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 50;
    log.textContent = call.events.map(event => JSON.stringify(event)).join('\n');
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  async function refresh() {
    try { render(await api('/api/call-lab')); notice(); }
    catch (error) { notice(error.message); $('labConnectionText').textContent = 'Dashboard disconnected'; $('labConnection').dataset.state = ''; }
    if (!stopped) setTimeout(refresh, 1000);
  }

  async function save() {
    pending = true;
    if (saving) return;
    saving = true;
    try {
      while (pending) {
        pending = false;
        const id = selected, payload = {};
        for (const [field, key] of Object.entries(fields)) {
          if (!$(field).checkValidity()) throw new Error(`Check ${$(field).previousElementSibling.textContent}`);
          payload[key] = $(field).type === 'checkbox' ? $(field).checked : Number($(field).value);
        }
        payload.response_enabled = $('callBehavior').value === 'conversation';
        if (payload.silence_threshold > payload.speech_threshold) throw new Error('Silence threshold must be at or below the speech threshold.');
        $('callSettingsStatus').textContent = 'Applying to the active call…';
        const ack = await api(`/api/call-lab/calls/${encodeURIComponent(id)}/configure`, { method: 'POST', body: JSON.stringify(payload) });
        if (id === selected) {
          $('callSettingsStatus').textContent = `Applied · revision ${ack.vad.revision}`;
          $('callSettingsStatus').dataset.state = 'applied';
        }
      }
    } catch (error) { $('callSettingsStatus').textContent = error.message; $('callSettingsStatus').dataset.state = 'error'; }
    finally { saving = false; }
  }

  async function networkRefresh() {
    if (!networkBusy) {
      try {
        const data = await api('/api/call-lab/network');
        $('networkPreset').disabled = !data.available;
        if (document.activeElement !== $('networkPreset')) $('networkPreset').value = data.preset;
        $('networkStatus').textContent = data.available ? `Applied: ${data.preset} · both RTP directions` : data.detail;
        $('networkScope').textContent = data.detail;
        $('networkStatus').dataset.state = data.available ? 'applied' : 'error';
      } catch (error) { $('networkPreset').disabled = true; $('networkStatus').textContent = error.message; }
    }
    if (!stopped) setTimeout(networkRefresh, 5000);
  }

  $('connectDashboard').addEventListener('click', async () => {
    token = $('labToken').value.trim();
    try { render(await api('/api/call-lab')); notice(); } catch (error) { notice(error.message); }
  });
  $('callSelect').addEventListener('change', () => { selected = $('callSelect').value; editing = false; if (lastSnapshot) render(lastSnapshot); });
  $('callControls').addEventListener('focusin', () => { editing = true; });
  $('callControls').addEventListener('focusout', () => { setTimeout(() => { editing = $('callControls').contains(document.activeElement); }, 0); });
  for (const id of [...Object.keys(fields), 'callBehavior']) $(id).addEventListener('change', save);
  $('hangup').addEventListener('click', async () => {
    $('hangup').disabled = true;
    try { await api(`/api/call-lab/calls/${encodeURIComponent(selected)}/hangup`, { method: 'POST' }); }
    catch (error) { notice(error.message); }
  });
  $('networkPreset').addEventListener('change', async () => {
    networkBusy = true; $('networkPreset').disabled = true;
    try {
      const data = await api('/api/call-lab/network', { method: 'POST', body: JSON.stringify({ preset: $('networkPreset').value }) });
      $('networkStatus').textContent = data.available ? `Applied: ${data.preset} · both RTP directions` : data.detail;
      $('networkPreset').value = data.preset;
    } catch (error) { $('networkStatus').textContent = error.message; }
    finally { networkBusy = false; }
  });
  window.addEventListener('pagehide', event => { if (!event.persisted) stopped = true; });
  refresh(); networkRefresh();
})();
