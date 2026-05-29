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
  sop: null,            // 已解析的 SOP（来自后端 /sop/validate）
  sopPayload: null,     // 原始用户输入（在 setup 时透传）
  snapshot: null,       // 最近一次的 assessor.snapshot()
  violationCount: 0,
  serverConfig: {},
};

const ui = {
  wsUrl: $("wsUrl"),
  model: $("model"),
  languageCode: $("languageCode"),
  voiceCoach: $("voiceCoach"),
  mockMode: $("mockMode"),
  connectBtn: $("connectBtn"),
  disconnectBtn: $("disconnectBtn"),
  micBtn: $("micBtn"),
  cameraBtn: $("cameraBtn"),
  requestStatusBtn: $("requestStatusBtn"),
  preview: $("preview"),
  sopInput: $("sopInput"),
  parseSopBtn: $("parseSopBtn"),
  loadSampleBtn: $("loadSampleBtn"),
  clearSopBtn: $("clearSopBtn"),
  sopName: $("sopName"),
  sopScoreSummary: $("sopScoreSummary"),
  stepList: $("stepList"),
  currentStepCard: $("currentStepCard"),
  scoreEarned: $("scoreEarned"),
  scoreMax: $("scoreMax"),
  stepsCovered: $("stepsCovered"),
  violationCount: $("violationCount"),
  assessmentLog: $("assessmentLog"),
  transcripts: $("transcripts"),
  eventLog: $("eventLog"),
  statusDot: $("statusDot"),
  statusText: $("statusText"),
  latencyText: $("latencyText"),
};

const STATUS_CN = {
  pending: "待执行",
  in_progress: "进行中",
  completed: "已完成",
  skipped: "已跳过",
  failed: "失败",
};

const STATUS_VARIANT = {
  pending: "pending",
  in_progress: "active",
  completed: "ok",
  skipped: "warn",
  failed: "err",
};

const SAMPLE_SOP = {
  sop_name: "坐姿端正与面部动作检测",
  total_scoring_points: 10,
  steps: [
    {
      id: 1,
      step_name: "保持端正坐姿",
      description: "上身坐直，肩膀平衡，头部保持正向，不要长时间驼背或歪头。",
      scoring_criteria: "连续 5 秒保持躯干挺直，头部基本居中。",
      deduction_rule: "明显驼背扣 2 分；长时间歪头扣 1 分；趴桌扣 2 分。",
      keywords_required: ["我已坐直", "保持端正"],
      ai_recognition_clues: "检测肩线是否水平、颈部是否前倾、头部相对屏幕中心偏移量。",
      forbidden_action: ["驼背", "趴桌", "歪头超过5秒"],
      weight: 4,
    },
    {
      id: 2,
      step_name: "摇头动作验证",
      description: "按指令完成 1 次清晰摇头（左右各一次）。",
      scoring_criteria: "头部左右摆动明显，动作完整。",
      deduction_rule: "动作幅度过小扣 1 分；只完成单侧扣 1 分。",
      keywords_required: ["开始摇头", "摇头完成"],
      ai_recognition_clues: "检测头部关键点左右位移轨迹与速度变化。",
      forbidden_action: ["持续低头不做动作"],
      weight: 3,
    },
    {
      id: 3,
      step_name: "张嘴动作验证",
      description: "按指令完成 1 次清晰张嘴并恢复闭嘴。",
      scoring_criteria: "嘴部开合明显，开口与闭口状态均可识别。",
      deduction_rule: "仅微张口扣 1 分；未完成闭口恢复扣 1 分。",
      keywords_required: ["开始张嘴", "张嘴完成"],
      ai_recognition_clues: "检测嘴唇关键点垂直距离的明显增大与回落。",
      forbidden_action: ["遮挡嘴部导致无法识别"],
      weight: 3,
    },
  ],
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
  // 先绑定事件，避免配置接口慢/失败时出现“按钮点击无反应”。
  bindEvents();
  await loadServerConfig();
  // 默认填入一份可运行 SOP，降低“点击没反应”的首用门槛。
  ui.sopInput.value = JSON.stringify(SAMPLE_SOP, null, 2);
  await parseSop();
  window.addEventListener("error", (event) => {
    logEvent({ type: "error", message: `前端运行时错误: ${event.message}` });
    setStatus("前端异常", event.message || "请查看事件日志", "error");
  });
}

async function loadServerConfig() {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    const response = await fetch("/api/gemini-live/config", { signal: controller.signal });
    clearTimeout(timer);
    const body = await response.json();
    const config = body.data || {};
    state.serverConfig = config;
    ui.model.value = config.model || "gemini-live-2.5-flash-native-audio";
    ui.languageCode.value = config.languageCode || "zh-CN";
    if (typeof config.voiceCoachDefault === "boolean") {
      ui.voiceCoach.checked = config.voiceCoachDefault;
    }
  } catch (error) {
    ui.model.value = "gemini-live-2.5-flash-native-audio";
    if (!ui.voiceCoach.checked) {
      // 配置加载失败时，为了便于本地联调，默认开启语音教练。
      ui.voiceCoach.checked = true;
    }
    logEvent({
      type: "error",
      message: `加载服务端配置失败，已使用本地默认值: ${error?.message || "unknown error"}`,
    });
  }
}

function bindEvents() {
  on(ui.connectBtn, "click", connect);
  on(ui.disconnectBtn, "click", disconnect);
  on(ui.micBtn, "click", toggleMic);
  on(ui.cameraBtn, "click", toggleCamera);
  on(ui.requestStatusBtn, "click", requestStatus);
  on(ui.parseSopBtn, "click", parseSop);
  on(ui.loadSampleBtn, "click", () => {
    ui.sopInput.value = JSON.stringify(SAMPLE_SOP, null, 2);
    parseSop();
  });
  on(ui.clearSopBtn, "click", clearSop);
}

// =========================================================================
// SOP 输入/解析
// =========================================================================

async function parseSop() {
  const raw = ui.sopInput.value.trim();
  if (!raw) {
    logEvent({ type: "error", message: "请先粘贴或载入 SOP" });
    setStatus("缺少 SOP", "请先粘贴 SOP，再点击“解析并预览”", "error");
    return;
  }
  // 后端 /sop/validate 接受 dict（先 JSON.parse 失败则原样作为字符串）
  let sopForServer;
  try {
    sopForServer = JSON.parse(raw);
  } catch {
    sopForServer = raw; // markdown 字符串
  }

  try {
    const response = await fetch("/api/gemini-live/sop/validate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ sop: sopForServer }),
    });
    const body = await response.json();
    if (!response.ok || !body?.data?.ok) {
      const message = body?.message || body?.data?.message || "SOP 解析失败";
      logEvent({ type: "error", message });
      ui.sopName.textContent = `解析失败：${message}`;
      ui.sopName.className = "sop-name err";
      setStatus("SOP 解析失败", message, "error");
      return;
    }
    state.sop = body.data;
    state.sopPayload = sopForServer;
    renderSopMeta();
    renderStepList(buildInitialSnapshot(body.data));
    logEvent({
      type: "local",
      message: `SOP 解析成功：${body.data.sopName}（${body.data.stepCount} 步，满分 ${body.data.totalScoringPoints}）`,
    });
    setStatus("SOP 已就绪", `共 ${body.data.stepCount} 步，可开始实训`, "idle");
  } catch (error) {
    logEvent({ type: "error", message: `SOP 校验请求失败: ${error.message}` });
    setStatus("SOP 校验请求失败", error.message, "error");
  }
}

function clearSop() {
  ui.sopInput.value = "";
  state.sop = null;
  state.sopPayload = null;
  state.snapshot = null;
  ui.sopName.textContent = "尚未解析";
  ui.sopName.className = "sop-name";
  ui.sopScoreSummary.textContent = "— / —";
  ui.stepList.innerHTML = "";
  ui.currentStepCard.textContent = "尚未开始";
  ui.currentStepCard.className = "current-step muted";
  ui.scoreEarned.textContent = "0";
  ui.scoreMax.textContent = "0";
  ui.stepsCovered.textContent = "0 / 0";
  ui.violationCount.textContent = "0";
  state.violationCount = 0;
}

function renderSopMeta() {
  if (!state.sop) {
    return;
  }
  ui.sopName.textContent = state.sop.sopName;
  ui.sopName.className = "sop-name ok";
  ui.sopScoreSummary.textContent = `满分 ${state.sop.totalScoringPoints} · ${state.sop.stepCount} 步`;
  ui.scoreMax.textContent = String(state.sop.totalScoringPoints);
}

function buildInitialSnapshot(sop) {
  return {
    sopName: sop.sopName,
    currentStepId: null,
    totalScore: 0,
    totalMax: sop.totalScoringPoints,
    steps: sop.steps.map((s) => ({
      stepId: s.id,
      name: s.name,
      weight: s.weight,
      status: "pending",
      scoreEarned: 0,
      kopsLogged: [],
      deductions: [],
      keywordHits: [],
    })),
  };
}

function renderStepList(snapshot) {
  state.snapshot = snapshot;
  ui.stepList.innerHTML = "";
  const currentId = snapshot.currentStepId;
  snapshot.steps.forEach((step) => {
    const li = document.createElement("li");
    const variant = STATUS_VARIANT[step.status] || "pending";
    const isCurrent = String(step.stepId) === String(currentId);
    li.className = `step-item ${variant}${isCurrent ? " current" : ""}`;
    const header = document.createElement("div");
    header.className = "step-head";
    header.innerHTML = `
      <span class="step-id">#${step.stepId}</span>
      <span class="step-name"></span>
      <span class="step-weight">${formatScore(step.scoreEarned)}/${step.weight}</span>
      <span class="step-badge"></span>
    `;
    header.querySelector(".step-name").textContent = step.name;
    header.querySelector(".step-badge").textContent = STATUS_CN[step.status] || step.status;
    li.appendChild(header);

    if (step.kopsLogged.length || step.keywordHits.length || step.deductions.length) {
      const meta = document.createElement("div");
      meta.className = "step-meta";
      if (step.kopsLogged.length) {
        meta.appendChild(buildChipRow("KOP", step.kopsLogged));
      }
      if (step.keywordHits.length) {
        meta.appendChild(buildChipRow("关键词", step.keywordHits, "kw"));
      }
      if (step.deductions.length) {
        meta.appendChild(buildChipRow("扣分", step.deductions, "err"));
      }
      li.appendChild(meta);
    }
    ui.stepList.appendChild(li);
  });

  ui.scoreEarned.textContent = formatScore(snapshot.totalScore);
  ui.scoreMax.textContent = formatScore(snapshot.totalMax);
  const covered = snapshot.steps.filter(
    (s) => s.status === "completed" || s.status === "failed",
  ).length;
  ui.stepsCovered.textContent = `${covered} / ${snapshot.steps.length}`;

  if (currentId) {
    const cur = snapshot.steps.find((s) => String(s.stepId) === String(currentId));
    if (cur) {
      ui.currentStepCard.textContent = `当前：#${cur.stepId} ${cur.name}`;
      ui.currentStepCard.className = "current-step active";
    }
  } else if (state.client?.connected) {
    ui.currentStepCard.textContent = "全部步骤已结束";
    ui.currentStepCard.className = "current-step done";
  }
}

function buildChipRow(label, items, extraClass = "") {
  const row = document.createElement("div");
  row.className = `chip-row ${extraClass}`;
  const tag = document.createElement("span");
  tag.className = "chip-label";
  tag.textContent = label;
  row.appendChild(tag);
  items.forEach((item) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = item;
    row.appendChild(chip);
  });
  return row;
}

function formatScore(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return "0";
  }
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

// =========================================================================
// 连接 / 断开
// =========================================================================

function connect() {
  if (!state.sopPayload) {
    const message = "请先点击“解析并预览”确保 SOP 生效，再开始实训";
    logEvent({ type: "error", message });
    setStatus("无法开始", message, "error");
    return;
  }
  let url;
  try {
    url = new URL(ui.wsUrl.value);
  } catch {
    const message = `WebSocket 地址无效：${ui.wsUrl.value || "(空)"}`;
    logEvent({ type: "error", message });
    setStatus("地址错误", message, "error");
    return;
  }
  if (ui.mockMode.checked) {
    url.searchParams.set("mock", "true");
  }

  state.client = new GeminiLiveClient(url.toString());
  state.client.addEventListener("status", ({ detail }) => handleStatus(detail));
  state.client.addEventListener("event", ({ detail }) => logEvent(detail));
  state.client.addEventListener("text", ({ detail }) => addTranscript("Gemini", detail.text));
  state.client.addEventListener("transcription", ({ detail }) => {
    const label = detail.source === "input" ? "学员口述" : "教练旁白";
    addTranscript(label, detail.text);
  });
  state.client.addEventListener("audio", ({ detail }) => state.player.playBase64(detail.data));
  state.client.addEventListener("interrupted", () => {
    state.player.clear();
    addTranscript("系统", "检测到打断，已清空模型音频缓冲。");
  });
  state.client.addEventListener("assessment", ({ detail }) =>
    handleAssessmentEvent(detail.assessment || detail),
  );
  state.client.addEventListener("sop_ready", ({ detail }) => {
    logEvent({
      type: "local",
      message: `服务端已就绪：${detail.sopName}（共 ${detail.stepCount} 步，日志 ${detail.logPath}）`,
    });
    if (detail.snapshot) {
      renderStepList(detail.snapshot);
    }
  });
  state.client.addEventListener("snapshot", ({ detail }) => {
    if (detail.snapshot) {
      renderStepList(detail.snapshot);
    }
  });
  state.client.addEventListener("final_summary", ({ detail }) =>
    handleFinalSummary(detail.summary || detail),
  );
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
  ui.micBtn.textContent = "启动麦克风";
  ui.cameraBtn.textContent = "启动摄像头";
  setStatus("未连接", "已断开", "idle");
  logEvent({ type: "local", message: "已结束实训会话" });
}

function buildSetupPayload() {
  return {
    sop: state.sopPayload,
    model: ui.model.value.trim(),
    languageCode: ui.languageCode.value.trim() || "zh-CN",
    voiceCoach: ui.voiceCoach.checked,
  };
}

// =========================================================================
// 麦克风 / 摄像头
// =========================================================================

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

function requestStatus() {
  if (!state.client?.connected) {
    setStatus("未连接", "请先开始实训，再执行复查指针", "error");
    return;
  }
  state.client.send({ type: "request_status" });
  logEvent({ type: "local", message: "已请求服务端复查指针并提示模型自检" });
}

// =========================================================================
// 评估事件
// =========================================================================

function handleAssessmentEvent(detail) {
  if (!detail) {
    return;
  }
  if (detail.snapshot) {
    renderStepList(detail.snapshot);
  }

  const item = document.createElement("div");
  item.className = `assessment-item ${detail.kind}`;

  if (detail.kind === "step_event") {
    const sev = severityClass(detail.status, detail.errorSeverity);
    item.classList.add(sev);
    const head = document.createElement("div");
    head.className = "row";
    head.innerHTML = `
      <span class="badge">${detail.eventType}</span>
      <span class="step-ref"></span>
      <span class="status-pill ${detail.status}"></span>
    `;
    head.querySelector(".step-ref").textContent = `#${detail.stepId} ${detail.stepName || ""}`;
    head.querySelector(".status-pill").textContent = detail.status;
    item.appendChild(head);

    if (detail.kopName) {
      const kop = document.createElement("div");
      kop.className = "kop-row";
      kop.textContent = `KOP：${detail.kopName}${detail.weightTotal != null ? `（满分 ${detail.weightTotal}）` : ""}`;
      if (detail.scoreEarned != null && detail.weightTotal != null) {
        kop.textContent += ` · 得分 ${detail.scoreEarned}/${detail.weightTotal}`;
      }
      item.appendChild(kop);
    }

    const desc = document.createElement("div");
    desc.className = "desc";
    desc.textContent = detail.description || "";
    item.appendChild(desc);

    if (detail.evidenceClue) {
      const clue = document.createElement("small");
      clue.className = "meta";
      clue.textContent = `证据：${detail.evidenceClue}`;
      item.appendChild(clue);
    }
    if (Array.isArray(detail.deductionReasons) && detail.deductionReasons.length) {
      const ded = document.createElement("ul");
      ded.className = "deductions";
      for (const reason of detail.deductionReasons) {
        const li = document.createElement("li");
        li.textContent = reason;
        ded.appendChild(li);
      }
      item.appendChild(ded);
    }
  } else if (detail.kind === "keyword_hit") {
    item.classList.add("kw");
    const head = document.createElement("div");
    head.className = "row";
    head.innerHTML = `
      <span class="badge">关键词命中</span>
      <span class="step-ref"></span>
      <span class="kw-word"></span>
    `;
    head.querySelector(".step-ref").textContent = `#${detail.stepId}`;
    head.querySelector(".kw-word").textContent = detail.keyword;
    item.appendChild(head);
    if (detail.matchedPhrase) {
      const phrase = document.createElement("small");
      phrase.className = "meta";
      phrase.textContent = `原句：${detail.matchedPhrase}`;
      item.appendChild(phrase);
    }
  } else if (detail.kind === "forbidden_action") {
    item.classList.add("err");
    state.violationCount += 1;
    ui.violationCount.textContent = String(state.violationCount);
    const head = document.createElement("div");
    head.className = "row";
    head.innerHTML = `
      <span class="badge danger">违规</span>
      <span class="step-ref"></span>
      <span class="status-pill error"></span>
    `;
    head.querySelector(".step-ref").textContent = `#${detail.stepId}`;
    head.querySelector(".status-pill").textContent = detail.severity || "high";
    item.appendChild(head);
    const desc = document.createElement("div");
    desc.className = "desc";
    desc.textContent = detail.violation;
    item.appendChild(desc);
    if (detail.evidenceClue) {
      const clue = document.createElement("small");
      clue.className = "meta";
      clue.textContent = `证据：${detail.evidenceClue}`;
      item.appendChild(clue);
    }
  } else {
    item.textContent = JSON.stringify(detail);
  }

  const time = document.createElement("small");
  time.className = "ts";
  time.textContent = detail.timestamp || new Date().toLocaleTimeString();
  item.appendChild(time);

  ui.assessmentLog.prepend(item);
  while (ui.assessmentLog.children.length > 200) {
    ui.assessmentLog.removeChild(ui.assessmentLog.lastChild);
  }
}

function severityClass(status, severity) {
  if (status === "error") {
    return "err";
  }
  if (status === "warning") {
    return "warn";
  }
  if (severity === "critical" || severity === "high") {
    return "err";
  }
  if (severity === "medium") {
    return "warn";
  }
  return "ok";
}

function handleFinalSummary(summary) {
  if (!summary) {
    return;
  }
  if (summary.steps) {
    renderStepList(summary);
  }
  const elapsed = summary.elapsedSec ? `${summary.elapsedSec.toFixed(0)} 秒` : "";
  const item = document.createElement("div");
  item.className = "assessment-item summary";
  item.innerHTML = `
    <div class="row"><span class="badge">最终汇总</span></div>
    <div class="desc">
      用时 ${elapsed}，得分 ${formatScore(summary.totalScore)}/${formatScore(summary.totalMax)}
    </div>
    <small class="meta">日志：${summary.logPath || "(未记录)"}</small>
  `;
  ui.assessmentLog.prepend(item);
  setStatus("已结束", "实训会话结束", "idle");
}

// =========================================================================
// UI 辅助
// =========================================================================

function handleStatus(detail) {
  if (detail.status === "socket_open") {
    const latency = Math.round(performance.now() - state.connectedAt);
    setStatus("已连接", "WebSocket 已打开", "live");
    ui.latencyText.textContent = `连接耗时 ${latency} ms`;
    return;
  }
  if (detail.status === "setup_received") {
    setStatus("评估中", `语音教练：${detail.voiceCoach ? "开" : "关"}`, "live");
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
  ui.micBtn.disabled = !connected;
  ui.cameraBtn.disabled = !connected;
  ui.requestStatusBtn.disabled = !connected;
  ui.parseSopBtn.disabled = connected;
  ui.loadSampleBtn.disabled = connected;
  ui.clearSopBtn.disabled = connected;
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
  item.innerHTML = `<span class="tag"></span><span class="content"></span>`;
  item.querySelector(".tag").textContent = source;
  item.querySelector(".content").textContent = text;
  ui.transcripts.prepend(item);
  while (ui.transcripts.children.length > 100) {
    ui.transcripts.removeChild(ui.transcripts.lastChild);
  }
}

function logEvent(event) {
  const item = document.createElement("div");
  item.className = `event ${event.type || ""}`;
  const summary = summarizeEvent(event);
  item.innerHTML = `<span class="tag"></span><span class="content"></span>`;
  item.querySelector(".tag").textContent = event.type || "event";
  item.querySelector(".content").textContent = summary;
  ui.eventLog.prepend(item);
  while (ui.eventLog.children.length > 200) {
    ui.eventLog.removeChild(ui.eventLog.lastChild);
  }
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
  if (event.type === "assessment") {
    return `assessment.${event.assessment?.kind || "?"}`;
  }
  return JSON.stringify(event);
}

function on(element, eventName, handler) {
  if (!element) {
    logEvent({ type: "error", message: `页面元素缺失，无法绑定事件: ${eventName}` });
    return;
  }
  element.addEventListener(eventName, handler);
}
