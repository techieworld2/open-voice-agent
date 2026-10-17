"use strict";
(() => {
  const get = id => document.getElementById(id), dialog = get("tourDialog"), card = get("tourCard"), focus = get("tourFocus");
  const storageKey = "voice-console-tour-v1";
  const steps = [
    [".session-actions", "Start with a session", "Start microphone connects the WebSocket and asks for microphone permission. Stop session releases it. You can skip this guide at any step using Skip tour, or press Escape."],
    ["#mode", "Choose the audio profile", "Wideband sends 16kHz audio. Phone call simulation uses 8kHz audio and a telephone bandpass. Select the profile before connecting."],
    ["#responseMode", "Test speech without a working LLM", "Choose Transcription only to use STT without LLM or TTS requests. It works with local Whisper even when Gemini has reached its quota. You can change this during a session."],
    ["#echoCancellation", "Compare microphone processing live", "Open Microphone & processing to toggle echo cancellation, noise suppression, and automatic gain while speaking. The applied device settings tell you what your browser supports."],
    ["#monitorControls", "Hear the input sent to recognition", "After connecting, use Listen to my input with headphones. You hear server-received PCM after browser processing and codec conversion. Change the microphone switches to compare. Capture & review lets you replay a short sample."],
    ["#recognitionReadout", "Your transcript survives reply failures", "Recognized speech appears here and in the conversation before the LLM runs. LLM or TTS errors do not clear it. Local Whisper shows its final text after you pause and recognition finishes."],
    ["#vadPreset", "Tune voice activity and turn taking", "Start with an environment preset. Minimum speech rejects brief bursts; minimum silence controls how long a pause ends a turn. Expand the panel for descriptions of every setting."],
    ["#playbackBuffer", "Inspect agent playback", "Prebuffer trades startup delay for steadier audio. Pause holds queued audio; Flush cancels the reply and keeps only its confirmed spoken prefix in conversation history."],
    ["#benchmark", "Measure concurrent calls", "The benchmark starts 30 headless WebSocket callers using the selected profile and configured server providers. Run it with mock providers for offline timing tests."],
    [".events-panel .panel-heading", "Find the details when something fails", "Protocol events show STT progress, transcripts, provider errors, and interruption events. Replay this guide any time from Help & tour. Settings panels close again when you finish."],
  ];
  let index = 0, originalPanels = [], returnFocus, scrollPosition = 0, scheduled = false, hasStarted = false;
  function position() {
    scheduled = false; if (!dialog.open) return;
    const target = document.querySelector(steps[index][0]), rect = target.getBoundingClientRect(), margin = 12;
    const left = Math.max(4, rect.left - 6), top = Math.max(4, rect.top - 6);
    Object.assign(focus.style, {left: `${left}px`, top: `${top}px`, width: `${Math.max(0, Math.min(innerWidth - 4, rect.right + 6) - left)}px`, height: `${Math.max(0, Math.min(innerHeight - 4, rect.bottom + 6) - top)}px`});
    const width = card.offsetWidth, height = card.offsetHeight;
    let y = rect.bottom + margin;
    if (y + height > innerHeight - margin) y = rect.top - height - margin;
    if (y < margin) y = innerHeight - height - margin;
    card.style.left = `${Math.max(margin, Math.min(rect.left, innerWidth - width - margin))}px`;
    card.style.top = `${Math.max(margin, y)}px`;
  }
  function schedule() { if (dialog.open && !scheduled) { scheduled = true; requestAnimationFrame(position); } }
  function show() {
    originalPanels.forEach(([panel, open]) => { panel.open = open; });
    const [selector, title, description] = steps[index], target = document.querySelector(selector);
    const panel = target.closest("details"); if (panel) panel.open = true;
    get("tourProgress").textContent = `${index + 1} / ${steps.length}`;
    get("tourTitle").textContent = title; get("tourDescription").textContent = description;
    get("tourBack").disabled = index === 0; get("tourNext").textContent = index === steps.length - 1 ? "Finish" : "Next";
    target.scrollIntoView({behavior: "instant", block: "center", inline: "nearest"});
    position(); requestAnimationFrame(position);
  }
  function finish() {
    dialog.close(); originalPanels.forEach(([panel, open]) => { panel.open = open; });
    try { localStorage.setItem(storageKey, "seen"); } catch {}
    window.scrollTo({top: scrollPosition, behavior: "instant"}); returnFocus?.focus({preventScroll: true});
  }
  function start() {
    if (dialog.open) return;
    hasStarted = true;
    index = 0; returnFocus = document.activeElement; scrollPosition = scrollY;
    originalPanels = [...document.querySelectorAll("details.settings-group")].map(panel => [panel, panel.open]);
    dialog.showModal(); show(); get("tourNext").focus({preventScroll: true});
  }
  get("helpTour").onclick = start; get("tourSkip").onclick = finish;
  get("tourNext").onclick = () => { if (index === steps.length - 1) finish(); else { index++; show(); } };
  get("tourBack").onclick = () => { if (index > 0) { index--; show(); } };
  dialog.addEventListener("cancel", event => { event.preventDefault(); finish(); });
  window.addEventListener("resize", schedule); window.addEventListener("scroll", schedule, true);
  let seen = false; try { seen = localStorage.getItem(storageKey) === "seen"; } catch {}
  if (!seen) setTimeout(() => { if (!hasStarted) start(); }, 350);
})();
