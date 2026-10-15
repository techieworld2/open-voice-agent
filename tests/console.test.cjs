const {test} = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

function consoleHarness() {
  const nodes = new Map(), sent = [], timers = new Map(); let nextTimer = 0;
  const values = {speech: ".020", silence: ".015", minSpeech: "100", minSilence: "300", trailingSilence: "950", bargeSpeech: "320", vadPreset: "custom", monitorSource: "input"};
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, {value: values[id] || "", checked: ["semanticEot", "backchannels"].includes(id), dataset: {}, textContent: "", hidden: false});
    return nodes.get(id);
  };
  const scope = vm.createContext({
    document: {getElementById: element}, WebSocket: {OPEN: 1},
    requestAnimationFrame: () => 1,
    setTimeout: callback => { timers.set(++nextTimer, callback); return nextTimer; },
    clearTimeout: id => timers.delete(id),
    __socket: {readyState: 1, send: data => sent.push(JSON.parse(data))},
  });
  const code = fs.readFileSync(path.join(__dirname, "../static/console.js"), "utf8");
  const end = code.lastIndexOf("\nfor (const id of Object.keys(vadFields))");
  assert.ok(end > 0);
  vm.runInContext(code.slice(0, end), scope);
  const run = code => vm.runInContext(code, scope);
  run("ready = true; ws = __socket;");
  function tick() {
    for (const [id, callback] of [...timers]) { timers.delete(id); callback(); }
  }
  function ack(request, overrides = {}) {
    scope.__ack = {type: "vad.config.ack", request_id: request.request_id, vad: {...request, revision: request.request_id, ...overrides}};
    run("handleMessage(JSON.stringify(__ack))");
  }
  return {run, scope, element, sent, timers, tick, ack};
}

test("continuous dragging sends the latest settings during the same running session", () => {
  const h = consoleHarness();
  h.element("speech").value = ".030"; h.run('editVad("speech")');
  h.element("speech").value = ".040"; h.run('editVad("speech")');
  assert.equal(h.timers.size, 1);
  h.tick();
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].speech_threshold, .04);
  assert.equal(h.sent[0].type, "vad.config");
  h.element("speech").value = ".050"; h.run('editVad("speech")'); h.tick();
  assert.equal(h.sent.length, 2);
  assert.equal(h.sent[1].speech_threshold, .05);
  assert.equal(h.run("ws === __socket && ready"), true);
});

test("delayed acknowledgements never overwrite an unsent edit or a newer request", () => {
  const h = consoleHarness(); h.run("sendVad()"); const first = h.sent[0];
  h.element("speech").value = ".040"; h.run('editVad("speech")');
  h.ack(first);
  assert.equal(Number(h.element("speech").value), .04);
  h.tick(); const second = h.sent[1];
  h.ack(first);
  assert.equal(Number(h.element("speech").value), .04);
  h.ack(second);
  assert.equal(h.element("vadState").dataset.state, "applied");
  assert.match(h.element("appliedVad").textContent, /speech ≥0.040/);
  assert.equal(Number(h.element("speech").value), .04);
});

test("individual threshold edits keep their companion threshold valid and send live", () => {
  const h = consoleHarness();
  h.element("speech").value = ".010"; h.run('editVad("speech")'); h.tick();
  assert.equal(h.sent[0].silence_threshold, .009);
  assert.equal(h.sent[0].speech_threshold, .01);
  h.element("silence").value = ".080"; h.run('editVad("silence")'); h.tick();
  assert.equal(h.sent[1].speech_threshold, .081);
  assert.equal(h.sent[1].silence_threshold, .08);
});

test("an older server acknowledgement reports incompatibility instead of claiming success", () => {
  const h = consoleHarness(); h.run("sendVad()");
  h.scope.__ack = {type: "vad.config.ack", vad: {speech_threshold: .02}};
  h.run("handleMessage(JSON.stringify(__ack))");
  assert.equal(h.element("vadState").dataset.state, "error");
  assert.match(h.element("notice").textContent, /running server/);
  assert.equal(h.element("notice").hidden, false);
});

test("live VAD gate routes silence or audio for each exact frame without altering STT samples", () => {
  const h = consoleHarness(), output = [];
  const pcm = new ArrayBuffer(640), view = new DataView(pcm);
  for (let offset = 0; offset < 640; offset += 2) view.setInt16(offset, 8192, true);
  h.scope.__pcm = pcm; h.scope.__port = {postMessage: message => output.push(message)};
  h.run("context = {sampleRate:16000}; monitorPlayer = {port:__port}; monitoring = true;");
  h.element("monitorSource").value = "vad";
  h.run('handleMonitor(__pcm, {sample_rate:16000, encoding:"pcm_s16le", vad_speaking:false})');
  assert.ok(output.at(-1).samples.every(sample => sample === 0));
  h.run('handleMonitor(__pcm, {sample_rate:16000, encoding:"pcm_s16le", vad_speaking:true})');
  assert.ok(output.at(-1).samples.every(sample => sample === .25));
  h.element("monitorSource").value = "input";
  h.run('handleMonitor(__pcm, {sample_rate:16000, encoding:"pcm_s16le", vad_speaking:false})');
  assert.ok(output.at(-1).samples.every(sample => sample === .25));
  assert.equal(view.getInt16(0, true), 8192);
  assert.equal(h.run("monitorSamples"), 960);
});
