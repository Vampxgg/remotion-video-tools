import { GeminiLiveClient } from "./gemini-live-client.js";
import { PcmAudioPlayer } from "./audio-player.js";
import { MediaHandler } from "./media-handler.js";

const $ = (id) => document.getElementById(id);

const state = {
  client: null,
  media: null,
  player: new PcmAudioPlayer(24000),
  connectedAt: null,
  micOn: false,
  cameraOn: false,
};

const ui = {
  wsUrl: $("wsUrl"),
  model: $("model"),
  languageCode: $("languageCode"),
  systemInstruction: $("systemInstruction"),
  enableAudio: $("enableAudio"),
  enableText: $("enableText"),
  enableTranscription: $("enableTranscription"),
  enableAffectiveDialog: $("enableAffectiveDialog"),
  proactiveAudio: $("proactiveAudio"),
  mockMode: $("mockMode"),
  connectBtn: $("connectBtn"),
  disconnectBtn: $("disconnectBtn"),
  micBtn: $("micBtn"),
  cameraBtn: $("cameraBtn"),
  clearAudioBtn: $("clearAudioBtn"),
  sendTextBtn: $("sendTextBtn"),
  textInput: $("textInput"),
  textForm: $("textForm"),
  transcripts: $("transcripts"),
  eventLog: $("eventLog"),
  statusDot: $("statusDot"),
  statusText: $("statusText"),
  latencyText: $("latencyText"),
  preview: $("preview"),
};

init();

async function init() {
  const defaultWs = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/gemini-live/ws`;
  ui.wsUrl.value = location.host ? defaultWs : "ws://127.0.0.1:2906/api/gemini-live/ws";
  state.media = new MediaHandler({
    previewVideo: ui.preview,
    onAudioChunk: (chunk) => state.client?.sendAudio(chunk),
    onVideoFrame: (frame) => state.client?.sendVideo(frame),
  });

  await loadServerConfig();
  bindEvents();
}

async function loadServerConfig() {
  try {
    const response = await fetch("/api/gemini-live/config");
    const body = await response.json();
    const config = body.data || {};
    ui.model.value = config.model || "gemini-live-2.5-flash-native-audio";
    ui.languageCode.value = config.languageCode || "zh-CN";
    const modalities = config.responseModalities || ["audio"];
    ui.enableAudio.checked = modalities.includes("audio");
    ui.enableText.checked = modalities.includes("text");
    ui.enableTranscription.checked = Boolean(config.enableTranscription);
  } catch {
    ui.model.value = "gemini-live-2.5-flash-native-audio";
  }
}

function bindEvents() {
  ui.connectBtn.addEventListener("click", connect);
  ui.disconnectBtn.addEventListener("click", disconnect);
  ui.clearAudioBtn.addEventListener("click", () => {
    state.player.clear();
    logEvent({ type: "local", message: "已清空播放缓冲" });
  });
  ui.micBtn.addEventListener("click", toggleMic);
  ui.cameraBtn.addEventListener("click", toggleCamera);
  ui.textForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = ui.textInput.value.trim();
    if (!text || !state.client?.connected) {
      return;
    }
    addTranscript("用户", text);
    state.client.sendText(text);
    ui.textInput.value = "";
  });
}

function connect() {
  const url = new URL(ui.wsUrl.value);
  if (ui.mockMode.checked) {
    url.searchParams.set("mock", "true");
  }

  state.client = new GeminiLiveClient(url.toString());
  state.client.addEventListener("status", ({ detail }) => handleStatus(detail));
  state.client.addEventListener("event", ({ detail }) => logEvent(detail));
  state.client.addEventListener("text", ({ detail }) => addTranscript("Gemini", detail.text));
  state.client.addEventListener("transcription", ({ detail }) => {
    const label = detail.source === "input" ? "输入转写" : "输出转写";
    addTranscript(label, detail.text);
  });
  state.client.addEventListener("audio", ({ detail }) => state.player.playBase64(detail.data));
  state.client.addEventListener("interrupted", () => {
    state.player.clear();
    addTranscript("系统", "检测到打断，已清空模型音频缓冲。");
  });
  state.client.addEventListener("error", ({ detail }) => {
    setStatus("错误", detail.message, "error");
    logEvent({ type: "error", ...detail });
  });

  state.connectedAt = performance.now();
  state.client.connect(buildSetupPayload());
  setConnectedUi(true);
  setStatus("连接中", "正在建立 WebSocket", "idle");
}

function disconnect() {
  state.media.stopAll();
  state.micOn = false;
  state.cameraOn = false;
  state.client?.disconnect();
  state.client = null;
  state.player.clear();
  setConnectedUi(false);
  setStatus("未连接", "已断开", "idle");
}

function buildSetupPayload() {
  const responseModalities = [];
  if (ui.enableAudio.checked) {
    responseModalities.push("audio");
  }
  if (ui.enableText.checked) {
    responseModalities.push("text");
  }
  return {
    model: ui.model.value.trim(),
    languageCode: ui.languageCode.value.trim() || "zh-CN",
    systemInstruction: ui.systemInstruction.value.trim(),
    responseModalities,
    enableTranscription: ui.enableTranscription.checked,
    enableAffectiveDialog: ui.enableAffectiveDialog.checked,
    proactiveAudio: ui.proactiveAudio.checked,
  };
}

async function toggleMic() {
  try {
    if (state.micOn) {
      state.media.stopMic();
      state.client?.audioStreamEnd();
      state.micOn = false;
      ui.micBtn.textContent = "启动麦克风";
      return;
    }
    await state.media.startMic();
    state.micOn = true;
    ui.micBtn.textContent = "停止麦克风";
  } catch (error) {
    logEvent({ type: "error", message: `麦克风启动失败: ${error.message}` });
  }
}

async function toggleCamera() {
  try {
    if (state.cameraOn) {
      state.media.stopCamera();
      state.cameraOn = false;
      ui.cameraBtn.textContent = "启动摄像头";
      return;
    }
    await state.media.startCamera();
    state.cameraOn = true;
    ui.cameraBtn.textContent = "停止摄像头";
  } catch (error) {
    logEvent({ type: "error", message: `摄像头启动失败: ${error.message}` });
  }
}

function handleStatus(detail) {
  if (detail.status === "socket_open") {
    const latency = Math.round(performance.now() - state.connectedAt);
    setStatus("已连接", "WebSocket 已打开", "live");
    ui.latencyText.textContent = `连接耗时 ${latency} ms`;
    return;
  }
  if (detail.status === "socket_closed") {
    setConnectedUi(false);
    setStatus("未连接", "WebSocket 已关闭", "idle");
  }
}

function setConnectedUi(connected) {
  ui.connectBtn.disabled = connected;
  ui.disconnectBtn.disabled = !connected;
  ui.sendTextBtn.disabled = !connected;
  ui.micBtn.disabled = !connected;
  ui.cameraBtn.disabled = !connected;
}

function setStatus(title, subtitle, mode) {
  ui.statusText.textContent = title;
  ui.latencyText.textContent = subtitle;
  ui.statusDot.className = `dot ${mode || "idle"}`;
}

function addTranscript(source, text) {
  if (!text) {
    return;
  }
  const item = document.createElement("div");
  item.className = "message";
  item.innerHTML = `<span class="tag"></span><span></span>`;
  item.querySelector(".tag").textContent = source;
  item.querySelector("span:last-child").textContent = text;
  ui.transcripts.prepend(item);
}

function logEvent(event) {
  const item = document.createElement("div");
  item.className = "event";
  const summary = summarizeEvent(event);
  item.innerHTML = `<span class="tag"></span><span></span>`;
  item.querySelector(".tag").textContent = event.type || "event";
  item.querySelector("span:last-child").textContent = summary;
  ui.eventLog.prepend(item);
}

function summarizeEvent(event) {
  if (event.message) {
    return event.message;
  }
  if (event.status) {
    return event.status;
  }
  if (event.text) {
    return event.text;
  }
  if (event.type === "audio") {
    return `${event.mimeType || "audio"}，${event.data?.length || 0} base64 chars`;
  }
  return JSON.stringify(event);
}
