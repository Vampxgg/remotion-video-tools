export class GeminiLiveClient extends EventTarget {
  constructor(url) {
    super();
    this.url = url;
    this.ws = null;
  }

  get connected() {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  connect(setupPayload) {
    if (this.connected) {
      return;
    }
    this.ws = new WebSocket(this.url);
    this.ws.addEventListener("open", () => {
      this.emit("status", { status: "socket_open" });
      this.send({ type: "setup", ...setupPayload });
    });
    this.ws.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        this.emit("event", payload);
        this.emit(payload.type || "message", payload);
      } catch (error) {
        this.emit("error", { message: `无法解析服务端消息: ${error.message}` });
      }
    });
    this.ws.addEventListener("close", () => this.emit("status", { status: "socket_closed" }));
    this.ws.addEventListener("error", () => this.emit("error", { message: "WebSocket 连接错误" }));
  }

  disconnect() {
    if (!this.ws) {
      return;
    }
    if (this.connected) {
      this.send({ type: "close" });
    }
    this.ws.close();
    this.ws = null;
  }

  sendText(text) {
    this.send({ type: "text", text });
  }

  sendAudio(base64Pcm) {
    this.send({ type: "audio", mimeType: "audio/pcm;rate=16000", data: base64Pcm });
  }

  sendVideo(base64Jpeg) {
    this.send({ type: "video", mimeType: "image/jpeg", data: base64Jpeg });
  }

  audioStreamEnd() {
    this.send({ type: "audio_stream_end" });
  }

  send(payload) {
    if (!this.connected) {
      this.emit("error", { message: "WebSocket 未连接" });
      return;
    }
    this.ws.send(JSON.stringify(payload));
  }

  emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
