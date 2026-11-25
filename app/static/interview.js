// 候选人面试页。
//
// 三个独立职责，刻意不互相耦合：
//   1. 问答流（HTTP）
//   2. 摄像头抽帧 → 服务端本地分析（HTTP，3 秒一帧）
//   3. 语音转写（浏览器 Web Speech API）与停顿/语速统计
//
// 为什么抽帧走 HTTP 而不是 WebSocket：帧是"丢弃也无所谓"的数据，
// 用 HTTP 天然可丢可重发，不需要在 WS 上做背压；而且每帧独立、无顺序依赖。
// WS 只用来接收服务端的轮次变化（其实轮询也能做，这里 WS 是为了演示实时观察）。

const IID = window.__IID;
const CFG = window.__CFG || {};

const $ = (id) => document.getElementById(id);
const qaList = $("qaList");
const answerEl = $("answer");
const msgEl = $("msg");
const stepsEl = $("steps");
const videoEl = $("video");
const camOff = $("camOff");
const camBadge = $("camBadge");
const camNote = $("camNote");

let turns = [];        // 已知轮次
let submitting = false;
let frameTimer = null;
let lastFrameAt = 0;

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------
function renderTurns() {
  qaList.innerHTML = "";
  if (!turns.length) {
    qaList.innerHTML = '<div class="muted" style="font-size:12.5px">等待第一题…</div>';
    return;
  }
  const LABEL = { main: "问题", followup: "追问", pressure: "追问（加压）", variant: "追问（条件变更）" };
  turns.forEach((t) => {
    const answered = !!(t.answer || "").trim();
    const wrap = document.createElement("div");
    wrap.className = "qa" + (answered ? "" : " answer-empty");
    wrap.innerHTML = `
      <div class="qlabel">${LABEL[t.phase] || t.phase} · 第 ${t.idx + 1} 题</div>
      <div class="q"></div>
      <div class="a"></div>
    `;
    wrap.querySelector(".q").textContent = t.question;
    if (answered) wrap.querySelector(".a").textContent = t.answer;
    qaList.appendChild(wrap);
  });
  // 只滚动问答区，不滚整页（否则手机上会滚走输入框）
  qaList.scrollTop = qaList.scrollHeight;
  const pending = turns.find((t) => !(t.answer || "").trim());
  answerEl.placeholder = pending
    ? "回答这一题…"
    : "这一轮已提交，等待下一题…";
  answerEl.disabled = !pending;
  $("submitBtn").disabled = !pending || submitting;
  updateSteps();
}

function updateSteps() {
  const pending = turns.find((t) => !(t.answer || "").trim());
  if (!pending) {
    stepsEl.innerHTML = '<div class="muted" style="font-size:12px">等待下一题。</div>';
    return;
  }
  const rows = [
    ["prepare", "规划本轮考察点"],
    ["generate", "生成题目"],
    ["analyze", "抽取回答证据"],
    ["decide", "决定追问或进入下一题"],
  ];
  stepsEl.innerHTML = rows
    .map(([k, label]) => `<div class="step" data-k="${k}">${label}${pending.lastSteps ? "" : " · 待执行"}</div>`)
    .join("");
}

// ---------------------------------------------------------------------------
// 与服务端同步
// ---------------------------------------------------------------------------
async function load() {
  const r = await fetch(`/api/interview/${IID}`);
  const j = await r.json();
  if (!j.ok) {
    msgEl.textContent = j.error || "加载失败";
    return;
  }
  turns = j.turns || [];
  renderTurns();
  const answered = turns.filter((t) => (t.answer || "").trim()).length;
  $("hmeta").textContent =
    `provider ${j.provider} · 已答 ${answered}/${CFG.questions_target || "-"} 题 · ` +
    `状态 ${j.interview.status}`;
}

// ---------------------------------------------------------------------------
// 提交
// ---------------------------------------------------------------------------
async function submit() {
  const text = answerEl.value.trim();
  if (!text || submitting) return;
  submitting = true;
  $("submitBtn").disabled = true;
  msgEl.textContent = "处理中…";
  try {
    const r = await fetch(`/api/interview/${IID}/answer`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ answer: text, audio_ms: lastSpeechMs() }),
    });
    const j = await r.json();
    if (!j.ok) {
      msgEl.textContent = j.error || "提交失败";
      submitting = false;
      $("submitBtn").disabled = false;
      return;
    }
    answerEl.value = "";
    msgEl.textContent = "";
    if (j.steps && j.steps.length) {
      stepsEl.innerHTML = j.steps
        .map((s) => `<div class="step${s.ok ? "" : " bad"}">${escapeHtml(s.step)} · ${escapeHtml(s.detail || "")}</div>`)
        .join("");
    }
    if (j.action === "closing") {
      msgEl.innerHTML = '面试已结束，<a href="/report/' + IID + '">查看报告 →</a>';
    }
    await load();
  } catch (e) {
    msgEl.textContent = `提交失败（网络）：${e.message}。可以直接再点一次，重复提交不会重复计分。`;
  } finally {
    submitting = false;
    $("submitBtn").disabled = answerEl.disabled;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Enter 提交 + 中文输入法安全
// IME 组词时按回车是"确认候选词"，不能当成提交。
// `keyCode === 229` 是补 Safari/旧 WebKit 的事件顺序 bug（isComposing 可能为 false）。
answerEl.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey) return;
  e.preventDefault();
  if (e.isComposing || e.keyCode === 229) return;
  submit();
});
$("submitBtn").addEventListener("click", submit);

// ---------------------------------------------------------------------------
// 摄像头：3 秒抽一帧
// ---------------------------------------------------------------------------
let camStream = null;

$("camBtn").addEventListener("click", async () => {
  if (camStream) {
    stopCam();
    return;
  }
  try {
    camStream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: "user" },
      audio: false,
    });
    videoEl.srcObject = camStream;
    camOff.classList.add("hidden");
    camBadge.classList.remove("hidden");
    $("camBtn").textContent = "关闭摄像头";
    startFrames();
  } catch (e) {
    camNote.textContent = `无法打开摄像头：${e.name}。${
      e.name === "NotAllowedError"
        ? "请在浏览器地址栏的权限设置里允许摄像头（localhost 属于安全上下文，不需要 HTTPS）。"
        : ""
    }`;
  }
});

function stopCam() {
  if (frameTimer) clearInterval(frameTimer);
  frameTimer = null;
  if (camStream) camStream.getTracks().forEach((t) => t.stop());
  camStream = null;
  videoEl.srcObject = null;
  camOff.classList.remove("hidden");
  camBadge.classList.add("hidden");
  $("camBtn").textContent = "开启摄像头";
}

// 画一张 640x480 的 canvas 抽帧。缩放而非原分辨率：JPEG 太大，
// 服务端做的是粗粒度观察，320 宽足够，传输量小一个数量级。
const canvas = document.createElement("canvas");
canvas.width = 320;
canvas.height = 240;
const ctx2d = canvas.getContext("2d");

function startFrames() {
  if (frameTimer) clearInterval(frameTimer);
  frameTimer = setInterval(sendFrame, 3000);
  sendFrame();
}

let lastVision = null;

async function sendFrame() {
  if (!camStream || videoEl.readyState < 2) return;
  ctx2d.drawImage(videoEl, 0, 0, canvas.width, canvas.height);
  // toDataURL 是同步的，会阻塞主线程几十毫秒。这里 3 秒一次可接受；
  // 若要更高频，应改用 canvas.toBlob + 二进制上传。
  const data = canvas.toDataURL("image/jpeg", 0.72);
  try {
    const r = await fetch(`/api/interview/${IID}/frame`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ image: data }),
    });
    const j = await r.json();
    if (!j.ok) return;
    if (j.skipped) return;
    lastVision = j.frame;
    const f = j.frame || {};
    camBadge.textContent = f.face_found
      ? `${f.looking ? "✓ 看镜头" : "· 视线偏移"} · 动作 ${(f.motion * 100).toFixed(0)}`
      : "画面中未检测到人脸";
    if (j.away_for > 0) camBadge.textContent = `离开画面 ${j.away_for.toFixed(0)}s`;
  } catch (e) {
    // 帧丢了就丢了，不重试——重试会在网络差时堆积请求
  }
}

// ---------------------------------------------------------------------------
// 语音：Web Speech API 转写 + 停顿/语速
// ---------------------------------------------------------------------------
let recog = null;
let speechStart = 0;
let lastSpeechAt = 0;
let speechChars = 0;
let speechMs = 0;

function lastSpeechMs() {
  return speechMs || 0;
}

$("voiceBtn").addEventListener("click", () => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    $("voiceState").textContent = "此浏览器不支持语音转写（用 Chrome/Edge）";
    $("voiceState").className = "tag warn";
    return;
  }
  if (recog) {
    recog.stop();
    recog = null;
    $("voiceBtn").textContent = "🎙 开始说话";
    $("voiceState").textContent = "语音已停止";
    $("voiceState").className = "tag mute";
    return;
  }
  recog = new SR();
  recog.lang = "zh-CN";
  recog.continuous = true;
  recog.interimResults = true;

  recog.onresult = (ev) => {
    let interim = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      if (r.isFinal) {
        const txt = r[0].transcript;
        speechChars += txt.length;
        answerEl.value = (answerEl.value ? answerEl.value + " " : "") + txt;
        reportTranscript(txt);
      } else {
        interim += r[0].transcript;
      }
    }
    // 停顿检测：两次 final 之间超过 1.5 秒记一次停顿
    const now = Date.now();
    if (lastSpeechAt && now - lastSpeechAt > 1500) {
      reportPause(now - lastSpeechAt);
    }
    lastSpeechAt = now;
  };
  recog.onerror = (ev) => {
    $("voiceState").textContent = `语音出错：${ev.error}`;
    $("voiceState").className = "tag warn";
    recog = null;
    $("voiceBtn").textContent = "🎙 开始说话";
  };
  recog.onend = () => {
    // continuous 模式下浏览器也会自动断，这里自动续上
    if (recog) {
      try { recog.start(); } catch (e) {}
    }
  };
  recog.start();
  speechStart = Date.now();
  $("voiceBtn").textContent = "⏹ 停止说话";
  $("voiceState").textContent = "正在听…";
  $("voiceState").className = "tag ok";
});

// 每秒把"已说话字数/时长"上报，用于语速与稳定性
setInterval(() => {
  if (!recog || !speechStart) return;
  const ms = Date.now() - speechStart;
  if (ms < 1000 || speechChars === 0) return;
  const cps = (speechChars / (ms / 1000));
  speechMs = ms;
  fetch(`/api/interview/${IID}/audio`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind: "speed", value: cps }),
  }).catch(() => {});
}, 2000);

function reportTranscript(text) {
  fetch(`/api/interview/${IID}/audio`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind: "transcript", text }),
  }).catch(() => {});
}

function reportPause(ms) {
  fetch(`/api/interview/${IID}/audio`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind: "pause", value: ms }),
  }).catch(() => {});
}

// ---------------------------------------------------------------------------
// 参数面板
// ---------------------------------------------------------------------------
$("applyCfg").addEventListener("click", async () => {
  const patch = {
    questions_target: Number($("qTarget2").value),
    followup_max: Number($("followupMax2").value),
  };
  const r = await fetch(`/api/interview/${IID}/adjust`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
  const j = await r.json();
  $("pacingOut").textContent = j.ok ? "已应用，下一题生效。" : (j.error || "应用失败");
});

$("pacingBtn").addEventListener("click", async () => {
  const r = await fetch(`/api/interview/${IID}/pacing`);
  const j = await r.json();
  if (!j.ok) return;
  $("pacingOut").textContent = (j.hints || []).map((h) => "· " + h.text).join("\n");
});

$("finishBtn").addEventListener("click", async () => {
  if (!confirm("确定结束这场面试？结束后不能再答。")) return;
  await fetch(`/api/interview/${IID}/finish`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ reason: "面试官手动结束" }),
  });
  location.href = `/report/${IID}`;
});

// 页面卸载时释放摄像头，否则指示灯一直亮着
window.addEventListener("beforeunload", stopCam);

load();
// 5 秒轮询一次（WS 已可用，但轮询保证 WS 挂掉时页面也不假死）
setInterval(load, 5000);
