class PcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.parts = [];
    this.length = 0;
    this.chunkFrames = Math.round(sampleRate / 10);
  }

  process(inputs, outputs) {
    const input = inputs[0];
    const output = outputs[0];
    for (let channel = 0; channel < output.length; channel++) {
      const source = input[channel] || input[0];
      if (source) output[channel].set(source);
    }

    const mono = input[0];
    if (mono?.length) {
      this.parts.push(new Float32Array(mono));
      this.length += mono.length;
    }
    if (this.length >= this.chunkFrames) {
      const chunk = new Float32Array(this.length);
      let offset = 0;
      for (const part of this.parts) {
        chunk.set(part, offset);
        offset += part.length;
      }
      this.parts = [];
      this.length = 0;
      this.port.postMessage(chunk.buffer, [chunk.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-processor", PcmProcessor);
