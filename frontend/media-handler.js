export class MediaHandler {
  constructor({ onAudioChunk, onVideoFrame, previewVideo }) {
    this.onAudioChunk = onAudioChunk;
    this.onVideoFrame = onVideoFrame;
    this.previewVideo = previewVideo;
    this.audioContext = null;
    this.audioStream = null;
    this.videoStream = null;
    this.processor = null;
    this.videoTimer = null;
    this.canvas = document.createElement("canvas");
  }

  async startMic() {
    if (this.audioStream) {
      return;
    }
    this.audioStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    this.audioContext = new AudioContext();
    const source = this.audioContext.createMediaStreamSource(this.audioStream);
    this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);
    this.processor.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      const downsampled = downsample(input, this.audioContext.sampleRate, 16000);
      const pcm = floatToPcm16(downsampled);
      this.onAudioChunk(bytesToBase64(pcm));
    };
    source.connect(this.processor);
    this.processor.connect(this.audioContext.destination);
  }

  stopMic() {
    if (this.processor) {
      this.processor.disconnect();
      this.processor = null;
    }
    if (this.audioContext) {
      this.audioContext.close();
      this.audioContext = null;
    }
    stopStream(this.audioStream);
    this.audioStream = null;
  }

  async startCamera() {
    if (this.videoStream) {
      return;
    }
    this.videoStream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 768 }, height: { ideal: 768 } },
      audio: false,
    });
    this.previewVideo.srcObject = this.videoStream;
    await this.previewVideo.play();
    this.videoTimer = window.setInterval(() => this.captureFrame(), 1000);
  }

  stopCamera() {
    if (this.videoTimer) {
      window.clearInterval(this.videoTimer);
      this.videoTimer = null;
    }
    stopStream(this.videoStream);
    this.videoStream = null;
    this.previewVideo.srcObject = null;
  }

  captureFrame() {
    if (!this.previewVideo.videoWidth || !this.previewVideo.videoHeight) {
      return;
    }
    this.canvas.width = 768;
    this.canvas.height = 768;
    const ctx = this.canvas.getContext("2d");
    const sourceSize = Math.min(this.previewVideo.videoWidth, this.previewVideo.videoHeight);
    const sx = (this.previewVideo.videoWidth - sourceSize) / 2;
    const sy = (this.previewVideo.videoHeight - sourceSize) / 2;
    ctx.drawImage(
      this.previewVideo,
      sx,
      sy,
      sourceSize,
      sourceSize,
      0,
      0,
      this.canvas.width,
      this.canvas.height,
    );
    const dataUrl = this.canvas.toDataURL("image/jpeg", 0.85);
    this.onVideoFrame(dataUrl.split(",")[1]);
  }

  stopAll() {
    this.stopMic();
    this.stopCamera();
  }
}

function stopStream(stream) {
  if (!stream) {
    return;
  }
  for (const track of stream.getTracks()) {
    track.stop();
  }
}

function downsample(input, sourceRate, targetRate) {
  if (sourceRate === targetRate) {
    return input;
  }
  const ratio = sourceRate / targetRate;
  const length = Math.floor(input.length / ratio);
  const output = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.floor((i + 1) * ratio);
    let sum = 0;
    let count = 0;
    for (let j = start; j < end && j < input.length; j += 1) {
      sum += input[j];
      count += 1;
    }
    output[i] = count ? sum / count : 0;
  }
  return output;
}

function floatToPcm16(float32) {
  const bytes = new Uint8Array(float32.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < float32.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, float32[i]));
    view.setInt16(i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return bytes;
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}
