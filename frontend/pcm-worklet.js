// Audio worklets for Voice Live: PCM16, 24 kHz, mono in both directions.
//
// Capture converts the browser's Float32 frames to Int16 and posts them to the main
// thread. Playback keeps a ring buffer that the main thread pushes into, which is
// what makes barge-in instant: dropping queued audio is a pointer reset, not a
// teardown of the audio graph.

class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    const pcm16 = new Int16Array(channel.length);
    for (let i = 0; i < channel.length; i++) {
      const sample = Math.max(-1, Math.min(1, channel[i]));
      pcm16[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    this.port.postMessage(pcm16, [pcm16.buffer]);
    return true;
  }
}

// Roughly 20 seconds at 24 kHz. Generous, because the model can produce audio
// faster than real time and we would rather buffer than drop.
const RING_CAPACITY = 24000 * 20;

class PcmPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(RING_CAPACITY);
    this.readIndex = 0;
    this.writeIndex = 0;
    this.playing = false;

    this.port.onmessage = (event) => {
      const { command, payload } = event.data;

      if (command === "clear") {
        // Barge-in: discard everything not yet played.
        this.readIndex = 0;
        this.writeIndex = 0;
        if (this.playing) {
          this.playing = false;
          this.port.postMessage({ event: "stopped" });
        }
        return;
      }

      if (command === "push") {
        const pcm16 = new Int16Array(payload);
        for (let i = 0; i < pcm16.length; i++) {
          this.buffer[this.writeIndex % RING_CAPACITY] = pcm16[i] / 0x8000;
          this.writeIndex++;
        }
        if (!this.playing) {
          this.playing = true;
          this.port.postMessage({ event: "started" });
        }
      }
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0][0];
    if (!output) return true;

    for (let i = 0; i < output.length; i++) {
      if (this.readIndex < this.writeIndex) {
        output[i] = this.buffer[this.readIndex % RING_CAPACITY];
        this.readIndex++;
      } else {
        output[i] = 0; // underrun: emit silence, keep the node alive
        if (this.playing) {
          this.playing = false;
          this.port.postMessage({ event: "stopped" });
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
registerProcessor("pcm-player", PcmPlayerProcessor);
