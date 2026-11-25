// 控制台：维度权重编辑 + 创建面试 + 历史刷新
const DIMS = window.__DIMS || [];

// --- 维度勾选与权重 ---
const dimsBox = document.getElementById("dims");
let dimState = DIMS.map((d) => ({ key: d.key, name: d.name, weight: d.weight, on: true }));

function dimsTotal() {
  return dimState.filter((d) => d.on).reduce((s, d) => s + Number(d.weight || 0), 0);
}

function renderDims() {
  dimsBox.innerHTML = "";
  dimState.forEach((d) => {
    const row = document.createElement("div");
    row.className = "dim-row";
    row.innerHTML = `
      <input type="checkbox" ${d.on ? "checked" : ""} data-k="${d.key}" />
      <span>${d.name}</span>
      <input class="w mono" type="number" step="0.01" min="0" max="1"
             value="${Number(d.weight).toFixed(2)}" data-k="${d.key}" ${d.on ? "" : "disabled"} />
    `;
    dimsBox.appendChild(row);
  });
  const t = dimsTotal();
  // 权重之和不必等于 1：后端会按启用维度重新归一化。
  // 这里只提示，不拦截——面试官想给某维度 3 倍权重时不该被表单挡住。
  const hint = document.createElement("div");
  hint.className = "muted";
  hint.style.cssText = "font-size:11.5px;margin-top:4px";
  hint.textContent = `当前启用维度权重合计 ${t.toFixed(2)}（提交时会按启用的维度自动归一化）`;
  dimsBox.appendChild(hint);
}

dimsBox.addEventListener("change", (e) => {
  const k = e.target.dataset.k;
  const d = dimState.find((x) => x.key === k);
  if (!d) return;
  if (e.target.type === "checkbox") d.on = e.target.checked;
  else d.weight = Number(e.target.value) || 0;
  renderDims();
});
renderDims();

// --- 时间显示 ---
// 用 Intl 而不是手拼字符串：手拼会漏掉时区，服务器与浏览器不在一个时区时全错。
const fmt = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});
document.querySelectorAll("td[data-ts]").forEach((td) => {
  const ts = Number(td.dataset.ts);
  if (ts) td.textContent = fmt.format(new Date(ts * 1000));
});

// --- 简历文件 / 上一轮报告（接入层） ---
// 为什么在浏览器里读成 base64 再上传、而不是 multipart：
// 后端是纯 Starlette，没有文件上传中间件，也不想为了一个字段引 python-multipart
// 之外的依赖。base64 多占 33% 流量，但一份简历几十 KB，可以接受。
let resumeFileB64 = "";
let resumeFileName = "";
const fileInput = document.getElementById("resumeFile");
const fileHint = document.getElementById("fileHint");
const fileParsed = document.getElementById("fileParsed");

document.getElementById("pickFileBtn").addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", () => {
  const f = fileInput.files && fileInput.files[0];
  if (!f) return;
  if (f.size > 8 * 1024 * 1024) {
    fileHint.textContent = "文件超过 8MB，请压缩后再传";
    return;
  }
  resumeFileName = f.name;
  fileHint.textContent = `已选择 ${f.name}（${(f.size / 1024).toFixed(0)} KB）`;
  const rd = new FileReader();
  rd.onload = () => {
    // readAsDataURL 会带 data:...;base64, 前缀，后端能容忍，这里保留原样
    resumeFileB64 = String(rd.result || "");
    fileParsed.textContent = "文件已就绪，创建面试时会自动解析成技能/项目。";
  };
  rd.onerror = () => {
    fileHint.textContent = "读取文件失败，请重试或直接粘贴文本";
  };
  rd.readAsDataURL(f);
});

let priorReportJson = "";
const priorBox = document.getElementById("priorReport");
const priorParsed = document.getElementById("priorParsed");

priorBox.addEventListener("input", () => {
  const v = priorBox.value.trim();
  priorReportJson = v;
  if (!v) {
    priorParsed.textContent = "";
    return;
  }
  try {
    const j = JSON.parse(v);
    const rep = j.report || j;
    const bits = [];
    if (rep.overall != null) bits.push(`综合分 ${Number(rep.overall).toFixed(1)}`);
    const ins = rep.insufficient_names || [];
    if (ins.length) bits.push(`上轮未测出 ${ins.join("、")}`);
    priorParsed.textContent = bits.length ? `已识别：${bits.join("；")}` : "JSON 可解析，但没找到报告字段";
  } catch {
    priorParsed.textContent = "不是合法 JSON，创建时会被忽略";
  }
});

document.getElementById("loadPriorBtn").addEventListener("click", async () => {
  const id = document.getElementById("priorReportId").value.trim();
  if (!id) {
    priorParsed.textContent = "先填一个报告 ID";
    return;
  }
  priorParsed.textContent = "拉取中…";
  try {
    const r = await fetch(`/api/interview/${encodeURIComponent(id)}/report`);
    const j = await r.json();
    if (!j.ok) {
      priorParsed.textContent = j.error || "拉取失败";
      return;
    }
    priorBox.value = JSON.stringify(j.report, null, 2);
    priorBox.dispatchEvent(new Event("input"));
  } catch (err) {
    priorParsed.textContent = `拉取失败：${err.message}`;
  }
});

// --- 创建 ---
const form = document.getElementById("createForm");
const msg = document.getElementById("createMsg");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  msg.textContent = "创建中…";
  const enabled = dimState.filter((d) => d.on);
  if (!enabled.length) {
    msg.textContent = "至少要启用一个能力维度。";
    return;
  }
  const payload = {
    candidate_name: document.getElementById("candidate").value.trim(),
    job_role: document.getElementById("jobRole").value.trim(),
    resume_text: document.getElementById("resume").value.trim(),
    resume_filename: resumeFileName,
    resume_file_b64: resumeFileB64,
    prior_report_json: priorReportJson,
    config: {
      job_role: document.getElementById("jobRole").value.trim(),
      questions_target: Number(document.getElementById("qTarget").value),
      followup_max: Number(document.getElementById("followupMax").value),
      base_minutes: Number(document.getElementById("baseMinutes").value),
      max_minutes: Number(document.getElementById("maxMinutes").value),
      enabled_dimensions: enabled.map((d) => d.key),
      weights: Object.fromEntries(dimState.map((d) => [d.key, Number(d.weight)])),
      pressure_round: document.getElementById("pressureRound").checked,
      variant_round: document.getElementById("variantRound").checked,
      voice_enabled: document.getElementById("voiceEnabled").checked,
    },
  };
  try {
    const r = await fetch("/api/interview", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!j.ok) {
      msg.textContent = j.error || "创建失败";
      return;
    }
    location.href = `/interview/${j.interview_id}`;
  } catch (err) {
    msg.textContent = `创建失败：${err.message}`;
  }
});

document.getElementById("refreshBtn").addEventListener("click", () => location.reload());
