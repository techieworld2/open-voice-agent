const {test} = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

function processors(rate) {
  const registered = {};
  const scope = {
    sampleRate: rate, Int16Array, Float32Array, ArrayBuffer, DataView, Math,
    AudioWorkletProcessor: class {
      constructor() { this.events = []; this.port = {postMessage: message => this.events.push(message)}; }
    },
    registerProcessor: (name, type) => { registered[name] = type; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/audio-worklet.js"), "utf8"), scope);
  return registered;
}

for (const device of [44100, 48000]) {
  for (const wire of [8000, 16000]) {
    test(`capture ${device}Hz -> ${wire}Hz strict packets without drift`, () => {
      const Capture = processors(device)["pcm-capture"];
      const capture = new Capture({processorOptions: {rate: wire}});
      const input = new Float32Array(128).fill(.25);
      const output = [[new Float32Array(128)]];
      const blocks = 1000;
      for (let i = 0; i < blocks; i++) capture.process([[input]], output);
      const expected = Math.floor(Math.floor(blocks * 128 * wire / device) / (wire / 50));
      assert.equal(capture.events.length, expected);
      for (const event of capture.events) {
        assert.equal(event.buffer.byteLength, wire / 50 * 2);
        assert.equal(new DataView(event.buffer).getInt16(0, true), 8192);
      }
      assert.ok(output[0][0].every(value => value === 0));
    });
  }
}

test("clear flushes queued audio, records rendered watermark, rejects stale generations", () => {
  const Player = processors(48000)["pcm-player"];
  const player = new Player();
  const post = data => player.port.onmessage({data});
  post({type: "generation", id: "old"});
  post({type: "audio", generation: "old", samples: new Float32Array(960).fill(.5), start_sample: 0, end_sample: 160});
  const out = [[new Float32Array(128)]];
  player.process([], out);
  assert.ok(out[0][0].every(value => value === .5));
  post({type: "clear"});
  const clear = player.events.at(-1);
  assert.equal(clear.type, "cleared");
  assert.equal(clear.generation_id, "old");
  assert.equal(clear.played_samples, Math.floor(160 * 128 / 960));
  post({type: "audio", generation: "old", samples: new Float32Array(960).fill(.5), start_sample: 160, end_sample: 320});
  player.process([], out);
  assert.ok(out[0][0].every(value => value === 0));
  post({type: "generation", id: "new"});
  post({type: "audio", generation: "old", samples: new Float32Array(960).fill(.5)});
  player.process([], out);
  assert.ok(out[0][0].every(value => value === 0));
});

test("playback ACK confirms stream sample counts at queue drain", () => {
  const Player = processors(48000)["pcm-player"];
  const player = new Player();
  player.port.onmessage({data: {type: "generation", id: "g"}});
  player.port.onmessage({data: {type: "audio", generation: "g", samples: new Float32Array(960), start_sample: 0, end_sample: 160}});
  for (let i = 0; i < 8; i++) player.process([], [[new Float32Array(128)]]);
  assert.equal(player.events.at(-1).played_samples, 160);
  assert.equal(player.events.at(-1).generation_id, "g");
});

test("prebuffer waits for enough audio, but a short completed response always drains", () => {
  const Player = processors(48000)["pcm-player"], player = new Player();
  const post = data => player.port.onmessage({data});
  post({type: "settings", buffer_ms: 200});
  post({type: "generation", id: "g"});
  post({type: "audio", generation: "g", samples: new Float32Array(960).fill(.5), start_sample: 0, end_sample: 160});
  const out = [[new Float32Array(128)]];
  player.process([], out);
  assert.ok(out[0][0].every(value => value === 0));
  assert.equal(player.played, 0);
  post({type: "end", generation: "g"});
  player.process([], out);
  assert.ok(out[0][0].every(value => value === .5));
  for (let i = 0; i < 7; i++) player.process([], out);
  assert.equal(player.played, 160);
});

test("paused playback retains the queue without acknowledging unplayed samples", () => {
  const Player = processors(48000)["pcm-player"], player = new Player();
  const post = data => player.port.onmessage({data});
  post({type: "generation", id: "g"});
  post({type: "settings", paused: true});
  post({type: "audio", generation: "g", samples: new Float32Array(960).fill(.5), start_sample: 0, end_sample: 160});
  const out = [[new Float32Array(128)]];
  for (let i = 0; i < 16; i++) player.process([], out);
  assert.equal(player.played, 0);
  assert.equal(player.queuedSamples, 960);
  assert.ok(!player.events.some(event => event.type === "played"));
  post({type: "clear"});
  assert.equal(player.events.at(-1).played_samples, 0);
  assert.equal(player.queuedSamples, 0);
});

test("playback queue overflow is bounded and flushes with an accurate watermark", () => {
  const Player = processors(48000)["pcm-player"], player = new Player();
  const post = data => player.port.onmessage({data});
  post({type: "generation", id: "g"});
  post({type: "settings", paused: true});
  post({type: "audio", generation: "g", samples: new Float32Array(480001), start_sample: 0, end_sample: 80001});
  assert.ok(player.events.some(event => event.type === "overflow"));
  assert.equal(player.events.find(event => event.type === "cleared").played_samples, 0);
  assert.equal(player.queue.length, 0);
  assert.equal(player.generation, null);
});

test("capture sends strict silence frames after a file source ends", () => {
  const Capture = processors(48000)["pcm-capture"], capture = new Capture({processorOptions: {rate: 8000}});
  for (let i = 0; i < 8; i++) capture.process([], [[new Float32Array(128)]]);
  assert.equal(capture.events.length, 1);
  assert.equal(capture.events[0].buffer.byteLength, 320);
  assert.equal(capture.events[0].rms, 0);
  assert.ok(new Uint8Array(capture.events[0].buffer).every(value => value === 0));
});
