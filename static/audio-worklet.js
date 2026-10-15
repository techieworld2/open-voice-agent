class PCMCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.rate = options.processorOptions.rate;
    this.size = this.rate / 50;
    this.frame = new Int16Array(this.size);
    this.count = 0;
    this.phase = 0;
    this.sum = 0;
    this.weight = 0;
  }
  process(inputs, outputs) {
    for (const channel of outputs[0] || []) channel.fill(0);
    const input = inputs[0]?.[0] || outputs[0]?.[0];
    if (!input) return true;
    const ratio = sampleRate / this.rate;
    for (const value of input) {
      let remaining = 1;
      while (remaining > 1e-9) {
        const take = Math.min(remaining, ratio - this.phase);
        this.sum += value * take;
        this.weight += take;
        this.phase += take;
        remaining -= take;
        if (this.phase >= ratio - 1e-9) {
          const average = Math.max(-1, Math.min(1, this.sum / this.weight));
          this.frame[this.count++] = Math.round(average * (average < 0 ? 32768 : 32767));
          this.phase = this.sum = this.weight = 0;
          if (this.count === this.size) {
            const buffer = new ArrayBuffer(this.size * 2);
            const view = new DataView(buffer);
            let power = 0;
            for (let i = 0; i < this.size; i++) {
              view.setInt16(i * 2, this.frame[i], true);
              power += (this.frame[i] / 32768) ** 2;
            }
            this.port.postMessage({type: "mic", buffer, rms: Math.sqrt(power / this.size)}, [buffer]);
            this.count = 0;
          }
        }
      }
    }
    return true;
  }
}

class PCMPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.generation = null;
    this.played = 0;
    this.ackFrames = 0;
    this.queuedSamples = 0;
    this.bufferMs = 0;
    this.paused = false;
    this.playing = false;
    this.ended = false;
    this.underruns = 0;
    this.statusTicks = 0;
    this.port.onmessage = ({data}) => {
      if (data.type === "generation") {
        this.queue = []; this.offset = this.played = 0; this.generation = data.id;
        this.queuedSamples = 0; this.playing = this.ended = false; this.underruns = 0;
      } else if (data.type === "settings") {
        if (Number.isFinite(data.buffer_ms)) this.bufferMs = Math.max(0, Math.min(500, data.buffer_ms));
        if (typeof data.paused === "boolean") this.paused = data.paused;
      } else if (data.type === "end" && data.generation === this.generation) {
        this.ended = true;
      } else if (data.type === "clear") {
        this.port.postMessage({type: "cleared", generation_id: this.generation, played_samples: this.played});
        this.queue = []; this.offset = 0; this.generation = null;
        this.queuedSamples = 0; this.playing = false; this.ended = true; this.statusTicks = 32;
      } else if (data.type === "audio" && data.generation === this.generation) {
        this.queue.push(data);
        this.queuedSamples += data.samples.length;
        if (this.queuedSamples > sampleRate * 10) {
          this.port.postMessage({type: "cleared", generation_id: this.generation, played_samples: this.played});
          this.port.postMessage({type: "overflow", generation_id: this.generation});
          this.queue = []; this.offset = this.queuedSamples = 0; this.generation = null;
          this.playing = false; this.ended = true;
        }
      }
    };
  }
  process(_, outputs) {
    const out = outputs[0]?.[0];
    if (!out) return true;
    out.fill(0);
    let index = 0;
    if (!this.playing && this.queue.length && (this.ended || this.queuedSamples >= sampleRate * this.bufferMs / 1000)) this.playing = true;
    while (!this.paused && this.playing && index < out.length && this.queue.length) {
      const segment = this.queue[0];
      const length = Math.min(out.length - index, segment.samples.length - this.offset);
      out.set(segment.samples.subarray(this.offset, this.offset + length), index);
      index += length; this.offset += length;
      this.queuedSamples -= length;
      const fraction = this.offset / segment.samples.length;
      this.played = segment.start_sample + Math.floor((segment.end_sample - segment.start_sample) * fraction);
      if (this.offset >= segment.samples.length) {
        this.played = segment.end_sample;
        this.queue.shift(); this.offset = 0;
      }
    }
    if (this.playing && !this.queue.length) {
      this.playing = false;
      if (!this.ended) this.underruns++;
    }
    if (++this.statusTicks >= 32) {
      this.statusTicks = 0;
      this.port.postMessage({type: "playback.status", buffered_ms: this.queuedSamples / sampleRate * 1000,
                             underruns: this.underruns, paused: this.paused});
    }
    if (index && (++this.ackFrames >= 4 || !this.queue.length)) {
      this.ackFrames = 0;
      this.port.postMessage({type: "played", generation_id: this.generation, played_samples: this.played});
    }
    return true;
  }
}
registerProcessor("pcm-capture", PCMCapture);
registerProcessor("pcm-player", PCMPlayer);
