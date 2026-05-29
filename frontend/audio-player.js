export class PcmAudioPlayer {
  constructor(sampleRate = 24000) {
    this.sampleRate = sampleRate;
    this.audioContext = null;
    this.gainNode = null;
    this.gain = 1;
    this.nextStartTime = 0;
    this.sources = new Set();
  }

  async ensureContext() {
    if (!this.audioContext) {
      this.audioContext = new AudioContext();
      this.gainNode = this.audioContext.createGain();
      this.gainNode.gain.value = this.gain;
      this.gainNode.connect(this.audioContext.destination);
      this.nextStartTime = this.audioContext.currentTime;
    }
    if (this.audioContext.state === "suspended") {
      await this.audioContext.resume();
    }
  }

  setGain(value) {
    this.gain = Math.max(0, Math.min(2.5, Number(value) || 0));
    if (this.gainNode) {
      this.gainNode.gain.value = this.gain;
    }
  }

  async playBase64(base64Pcm) {
    await this.ensureContext();
    const bytes = base64ToBytes(base64Pcm);
    const samples = pcm16ToFloat32(bytes);
    const buffer = this.audioContext.createBuffer(1, samples.length, this.sampleRate);
    buffer.copyToChannel(samples, 0);

    const source = this.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gainNode || this.audioContext.destination);
    source.onended = () => this.sources.delete(source);

    const startAt = Math.max(this.audioContext.currentTime, this.nextStartTime);
    source.start(startAt);
    this.nextStartTime = startAt + buffer.duration;
    this.sources.add(source);
  }

  clear() {
    for (const source of this.sources) {
      try {
        source.stop();
      } catch {
        // Source may already be stopped.
      }
    }
    this.sources.clear();
    if (this.audioContext) {
      this.nextStartTime = this.audioContext.currentTime;
    }
  }
}

function base64ToBytes(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function pcm16ToFloat32(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const samples = new Float32Array(bytes.byteLength / 2);
  for (let i = 0; i < samples.length; i += 1) {
    samples[i] = Math.max(-1, Math.min(1, view.getInt16(i * 2, true) / 32768));
  }
  return samples;
}
