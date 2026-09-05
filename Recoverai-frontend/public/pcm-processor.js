// public/pcm-processor.js
// AudioWorklet processor: queues incoming Int16 PCM chunks (from the
// VoiceChatConsumer WS binary frames) and plays them back seamlessly.
// Posts {type: "ended"} back to the main thread once the queued audio has
// actually finished playing after an {type: "end"} signal was received —
// NOT the instant "end" arrives, since there's usually still buffered
// audio left to play. This is what makes barge-in timing correct.

class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.readOffset = 0;
    this.ended = false;

    this.port.onmessage = (e) => {
      const msg = e.data;

      if (msg instanceof ArrayBuffer) {
        const int16 = new Int16Array(msg);
        const float32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) {
          float32[i] = int16[i] / 32768;
        }
        this.queue.push(float32);
        this.ended = false;
        return;
      }

      if (msg && msg.type === "end") {
        this.ended = true;
        return;
      }

      if (msg && msg.type === "clear") {
        this.queue = [];
        this.readOffset = 0;
        this.ended = false;
        return;
      }
    };
  }

  process(inputs, outputs) {
    const output = outputs[0][0];
    if (!output) return true;

    let i = 0;
    while (i < output.length) {
      if (this.queue.length === 0) {
        // nothing left queued -- fill silence
        output.fill(0, i);
        if (this.ended) {
          this.port.postMessage({ type: "ended" });
          this.ended = false;
        }
        break;
      }

      const chunk = this.queue[0];
      const remaining = chunk.length - this.readOffset;
      const toCopy = Math.min(remaining, output.length - i);

      output.set(chunk.subarray(this.readOffset, this.readOffset + toCopy), i);
      i += toCopy;
      this.readOffset += toCopy;

      if (this.readOffset >= chunk.length) {
        this.queue.shift();
        this.readOffset = 0;
      }
    }

    return true;
  }
}

registerProcessor("pcm-processor", PCMProcessor);
