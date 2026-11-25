"""报告导出：JSON / Markdown / 可打印 HTML。

需求里验收标准是「能导出 AI 面试分析报告（含分数）」。三个出口各有明确用途，
不是三种格式都做一遍好看的：

- **JSON**：给机器读。下一场面试的「报告接入」就吃这个文件（app/intake/report.py
  的 `parse_report_json` 兼容裸对象和 `{"ok":true,"report":{...}}` 两种形状），
  也是审计/回归测试的输入。**字段全部来自 build_report()，不做二次加工**，
  这样导出的文件重新导进来，分数不会变。
- **Markdown**：给人读、能贴进飞书/Notion/邮件，也能 diff。适合发给面试官。
- **HTML**：给打印。面试官要 PDF 时就靠浏览器「打印 → 另存为 PDF」，
  所以这里单独排一版窄栏、去交互的静态页，而不是复用带 WebSocket 的 /report 页面。

一个刻意的取舍：**导出不做鉴权上下文**（没有登录概念）。这是内部演示系统，
真要商用，鉴权应该加在路由层（见 docs/ARCHITECTURE.md「商业化边界」），
而不是让每个导出函数各自记一遍——那样接口一多就必然漏。
"""

from __future__ import annotations

import json
from datetime import datetime
from html import escape

from app.config import DIMENSION_MAP

MD_MAX_ANSWER = 400


def _pct(v) -> str:
    return "—" if v is None else f"{v:.1f}"


def _money(v) -> str:
    if v is None:
        return "—"
    return f"¥{v:.4f}"


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# build_report() 把部分字段放在 interview 子对象里（candidate/duration_sec），
# 而报告页模板用的是顶层。导出要同时吃这两种形状——它既要处理 build_report()
# 的实时结果，也要处理别人手工拼的、或者早先版本导出的 JSON 文件。
def _candidate_name(r: dict) -> str:
    itv = r.get("interview") or {}
    return str(itv.get("candidate_name") or itv.get("candidate") or "（未填姓名）")


def _duration_sec(r: dict):
    v = r.get("duration_sec")
    if v is None:
        v = (r.get("interview") or {}).get("duration_sec")
    return v


def _generated_at(r: dict) -> str:
    g = r.get("generated_at")
    if isinstance(g, str) and g:
        return g
    if isinstance(g, (int, float)):
        return datetime.fromtimestamp(g).strftime("%Y-%m-%d %H:%M")
    return _stamp()


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def to_json(report: dict) -> str:
    """原样导出，缩进 2 空格，方便人肉 diff 和当 fixture 用。"""
    return json.dumps(report, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def to_markdown(report: dict) -> str:
    r = report or {}
    itv = r.get("interview") or {}
    risk = r.get("risk") or {}
    cost = r.get("cost") or {}
    verdict = r.get("verdict") or {}

    L: list[str] = []
    L.append(f"# AI 面试分析报告：{_candidate_name(r)}")
    L.append("")
    L.append(f"- 岗位：{itv.get('job_role') or '—'}")
    dur = _duration_sec(r)
    L.append(f"- 时长：{(dur or 0) / 60:.1f} 分钟" if dur else "- 时长：—")
    L.append(f"- 模型通道：{r.get('provider') or itv.get('provider') or '—'}")
    L.append(f"- 生成时间：{_generated_at(r)}")
    L.append("")
    L.append("## 结论")
    L.append("")
    if r.get("overall") is None:
        L.append(f"**未出综合分** —— {verdict.get('text') or '证据不足'}")
    else:
        L.append(f"**综合分 {_pct(r.get('overall'))}（{r.get('overall_label') or ''}）**")
        L.append("")
        L.append(verdict.get("text") or "")
    L.append("")
    L.append("> 本报告是面试过程的观察记录，供面试官参考，不作为录用与否的唯一依据。")
    L.append("")

    # 维度
    L.append("## 能力维度")
    L.append("")
    L.append("| 维度 | 分数 | 权重 | 说明 |")
    L.append("| --- | --- | --- | --- |")
    for d in r.get("dimensions") or []:
        score = _pct(d.get("score")) if d.get("status") == "scored" else "不评分"
        L.append(f"| {d.get('name')} | {score} | {d.get('weight')} | "
                 f"{(d.get('reason') or '').replace('|', '/')} |")
    L.append("")
    ins = r.get("insufficient_names") or []
    if ins:
        L.append(f"证据不足、未给分的维度：{'、'.join(ins)}。"
                 "这几项在报告里留空而不是给 0 分——0 分和没测出来是两件事。")
        L.append("")

    # 证据
    L.append("## 分数依据")
    L.append("")
    for d in r.get("dimensions") or []:
        ev = d.get("evidence") or []
        if not ev:
            continue
        L.append(f"### {d.get('name')}")
        for e in ev[:4]:
            L.append(f"- {e.get('text')}")
        L.append("")

    # 风险
    L.append("## 过程风险信号")
    L.append("")
    L.append(f"- 风险等级：{risk.get('level') or '—'}（{_pct(risk.get('score'))}）")
    for reason in risk.get("reasons") or []:
        L.append(f"- {reason}")
    vision = risk.get("vision") or {}
    if vision.get("available"):
        L.append(f"- 检测到人脸帧占比 {_pct((vision.get('face_ratio_avg') or 0) * 100)}%，"
                 f"有脸时看镜头比例 {_pct((vision.get('looking_ratio') or 0) * 100)}%，"
                 f"离开画面比例 {_pct((vision.get('away_ratio') or 0) * 100)}%")
    else:
        L.append("- 本场没有可用画面，视觉相关指标不予计算")
    audio = risk.get("audio_headline") or {}
    if audio:
        L.append(f"- 语速均值 {audio.get('speed_chars_per_sec') or '—'} 字/秒，"
                 f"停顿占比 {_pct((audio.get('pause_ratio') or 0) * 100)}%")
    L.append("")

    # 过程异常
    fb = r.get("fallback_summary") or []
    L.append("## 过程异常与兜底")
    L.append("")
    if fb:
        for f in fb:
            L.append(f"- **{f.get('title')}**：{f.get('desc')}（{f.get('detail')}）")
    else:
        L.append("- 本场未触发异常兜底")
    L.append("")

    # 时间线
    L.append("## 问答时间线")
    L.append("")
    for t in r.get("timeline") or []:
        L.append(f"**#{t.get('idx')} [{t.get('phase_label')}]** {t.get('question') or ''}")
        L.append("")
        ans = (t.get("answer") or "").strip()
        if not ans:
            L.append("_（未作答）_")
        else:
            L.append(ans[:MD_MAX_ANSWER] + ("…" if len(ans) > MD_MAX_ANSWER else ""))
        L.append("")

    # 输入材料
    mat = r.get("material") or {}
    res = mat.get("resume") or {}
    prior = mat.get("prior_report") or {}
    if res.get("skills") or res.get("projects") or prior:
        L.append("## 本次面试的输入材料")
        L.append("")
        if res.get("source"):
            n = res.get("chars")
            L.append(f"- 简历来源：{res['source']}" + (f"（{n} 字）" if n else ""))
        if res.get("years"):
            L.append(f"- 识别年限：约 {res['years']:g} 年")
        if res.get("skills"):
            L.append(f"- 识别技能：{'、'.join(map(str, res['skills']))}")
        if res.get("projects"):
            L.append(f"- 识别项目：{'；'.join(map(str, res['projects']))}")
        if res.get("llm_summary"):
            L.append(f"- 模型摘要：{res['llm_summary']}")
        if prior:
            bits = []
            if prior.get("overall") is not None:
                bits.append(f"综合分 {_pct(prior['overall'])}")
            if prior.get("insufficient_dims"):
                bits.append("上轮未测出：" + "、".join(map(str, prior["insufficient_dims"])))
            L.append("- 接上一轮：" + ("；".join(bits) or "（已接入）"))
        if res.get("warnings"):
            L.append(f"- 解析提示：{'；'.join(map(str, res['warnings']))}")
        L.append("")

    # 成本
    L.append("## 模型调用与成本")
    L.append("")
    L.append(f"- 调用次数：{cost.get('calls') or 0}")
    L.append(f"- 合计：{_money(cost.get('total_cny'))}")
    by_model = cost.get("by_model") or {}
    if by_model:
        L.append("")
        L.append("| 模型 | 费用 |")
        L.append("| --- | --- |")
        for m, v in by_model.items():
            cny = v.get("cny") if isinstance(v, dict) else v
            L.append(f"| {m} | {_money(cny)} |")
    by_purpose = cost.get("by_purpose") or {}
    if by_purpose:
        L.append("")
        L.append("| 用途 | 费用 |")
        L.append("| --- | --- |")
        for p, v in by_purpose.items():
            cny = v.get("cny") if isinstance(v, dict) else v
            L.append(f"| {p} | {_money(cny)} |")
    unpriced = cost.get("unpriced_models") or []
    if unpriced:
        L.append("")
        L.append(f"未登记价目、按 0 计的模型：{'、'.join(unpriced)}（这笔钱是真花了，只是没算进表里）")
    budget = r.get("budget") or {}
    if budget.get("limit_cny") is not None:
        L.append("")
        L.append(f"- 预算上限 {_money(budget.get('limit_cny'))}，"
                 f"已花 {_money(budget.get('spent_cny'))}")
        if budget.get("tripped"):
            L.append(f"- **本场有 {budget['tripped']} 次调用因超出预算被降级为规则化实现**："
                     "面试未中断，但那几轮题目/分析不是模型给的，成本表也只计真实调用。")
        else:
            L.append("- 未触发预算熔断")
    L.append("")

    # 时长建议（build_report 给的是字符串，早期版本给的是 {hints:[...]}，两种都吃）
    pacing = r.get("pacing")
    hints = pacing.get("hints") if isinstance(pacing, dict) else None
    if hints:
        L.append("## 节奏建议")
        L.append("")
        for h in hints:
            L.append(f"- {h}")
        L.append("")
    elif isinstance(pacing, str) and pacing.strip():
        L.append("## 节奏建议")
        L.append("")
        L.append(f"- {pacing.strip()}")
        if r.get("pacing_patch"):
            L.append(f"- 建议参数：`{json.dumps(r['pacing_patch'], ensure_ascii=False)}`")
        L.append("")

    # 局限
    L.append("## 这份报告不做什么")
    L.append("")
    for x in r.get("limitations") or []:
        L.append(f"- {x}")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# HTML（打印用）
# ---------------------------------------------------------------------------
_HTML_CSS = """
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 28px 56px; max-width: 780px; margin-inline: auto;
  font: 14px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  color: #111; background: #fff; }
h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: -.01em; }
h2 { font-size: 15px; margin: 26px 0 8px; padding-bottom: 5px; border-bottom: 1px solid #e5e7eb; }
h3 { font-size: 13px; margin: 14px 0 4px; color: #374151; }
.meta { color: #6b7280; font-size: 12px; margin-bottom: 18px; }
.meta span + span::before { content: " · "; }
.score { display: flex; align-items: baseline; gap: 10px; margin: 6px 0 10px; }
.score b { font-size: 36px; line-height: 1; }
.score span { color: #6b7280; font-size: 13px; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid #f0f1f3; }
th { color: #6b7280; font-weight: 600; }
td.num { font-variant-numeric: tabular-nums; }
.none { color: #9ca3af; }
.note { background: #f9fafb; border-left: 3px solid #d1d5db; padding: 9px 12px;
  font-size: 12.5px; color: #374151; margin: 10px 0; }
ol, ul { margin: 6px 0; padding-left: 20px; }
li { margin: 3px 0; }
.qa { margin-bottom: 12px; }
.qa .q { font-weight: 600; }
.qa .a { color: #374151; white-space: pre-wrap; }
@media print { body { padding: 0; max-width: none; } .noprint { display: none; } }
"""


def to_html(report: dict) -> str:
    """打印专用静态页。刻意不引外部 CSS/JS——离线打开、直接打印都成立。"""
    r = report or {}
    itv = r.get("interview") or {}
    risk = r.get("risk") or {}
    cost = r.get("cost") or {}
    verdict = r.get("verdict") or {}
    e = escape

    parts: list[str] = []
    parts.append("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">")
    parts.append(f"<title>AI 面试分析报告 - {e(_candidate_name(r))}</title>")
    parts.append(f"<style>{_HTML_CSS}</style></head><body>")

    parts.append(f"<h1>AI 面试分析报告：{e(_candidate_name(r))}</h1>")
    parts.append("<div class=\"meta\">")
    parts.append(f"<span>岗位 {e(str(itv.get('job_role') or '—'))}</span>")
    dur = _duration_sec(r)
    parts.append(f"<span>时长 {(dur or 0)/60:.1f} 分钟</span>" if dur else "<span>时长 —</span>")
    parts.append(f"<span>模型通道 {e(str(r.get('provider') or itv.get('provider') or '—'))}</span>")
    parts.append(f"<span>{e(_generated_at(r))}</span>")
    parts.append("</div>")

    parts.append("<h2>结论</h2>")
    if r.get("overall") is None:
        parts.append(f"<p><b>未出综合分</b> —— {e(str(verdict.get('text') or '证据不足'))}</p>")
    else:
        parts.append(
            "<div class=\"score\"><b>"
            + e(_pct(r.get("overall")))
            + "</b><span>"
            + e(str(r.get("overall_label") or ""))
            + " / 100</span></div>"
        )
        parts.append(f"<p>{e(str(verdict.get('text') or ''))}</p>")
    parts.append("<div class=\"note\">本报告是面试过程的观察记录，供面试官参考，"
                 "不作为录用与否的唯一依据。</div>")

    parts.append("<h2>能力维度</h2><table><thead><tr>"
                 "<th>维度</th><th>分数</th><th>权重</th><th>说明</th></tr></thead><tbody>")
    for d in r.get("dimensions") or []:
        if d.get("status") == "scored":
            score = f"<td class=\"num\">{e(_pct(d.get('score')))}</td>"
        else:
            score = "<td class=\"none\">不评分</td>"
        parts.append(
            f"<tr><td>{e(str(d.get('name')))}</td>{score}"
            f"<td class=\"num\">{e(str(d.get('weight')))}</td>"
            f"<td>{e(str(d.get('reason') or ''))}</td></tr>"
        )
    parts.append("</tbody></table>")
    ins = r.get("insufficient_names") or []
    if ins:
        parts.append(f"<div class=\"note\">证据不足、未给分：{e('、'.join(map(str, ins)))}。"
                     "这几项留空而不是 0 分——0 分和没测出来是两件事。</div>")

    parts.append("<h2>分数依据</h2>")
    for d in r.get("dimensions") or []:
        ev = d.get("evidence") or []
        if not ev:
            continue
        parts.append(f"<h3>{e(str(d.get('name')))}</h3><ul>")
        for x in ev[:4]:
            parts.append(f"<li>{e(str(x.get('text')))}</li>")
        parts.append("</ul>")

    parts.append("<h2>过程风险信号</h2><ul>")
    parts.append(f"<li>风险等级：{e(str(risk.get('level') or '—'))}"
                 f"（{e(_pct(risk.get('score')))}）</li>")
    for reason in risk.get("reasons") or []:
        parts.append(f"<li>{e(str(reason))}</li>")
    vision = risk.get("vision") or {}
    if vision.get("available"):
        parts.append(
            f"<li>人脸帧占比 {_pct((vision.get('face_ratio_avg') or 0) * 100)}%，"
            f"有脸时看镜头 {_pct((vision.get('looking_ratio') or 0) * 100)}%，"
            f"离开画面 {_pct((vision.get('away_ratio') or 0) * 100)}%</li>"
        )
    else:
        parts.append("<li>本场没有可用画面，视觉指标不予计算</li>")
    parts.append("</ul>")

    parts.append("<h2>过程异常与兜底</h2>")
    fb = r.get("fallback_summary") or []
    if fb:
        parts.append("<ul>")
        for f in fb:
            parts.append(f"<li><b>{e(str(f.get('title')))}</b>：{e(str(f.get('desc')))}"
                         f"（{e(str(f.get('detail')))}）</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>本场未触发异常兜底。</p>")

    parts.append("<h2>问答时间线</h2>")
    for t in r.get("timeline") or []:
        ans = (t.get("answer") or "").strip()
        parts.append("<div class=\"qa\">")
        parts.append(f"<div class=\"q\">#{e(str(t.get('idx')))} "
                     f"[{e(str(t.get('phase_label')))}] {e(str(t.get('question') or ''))}</div>")
        if ans:
            parts.append(f"<div class=\"a\">{e(ans)}</div>")
        else:
            parts.append("<div class=\"a none\">（未作答）</div>")
        parts.append("</div>")

    # 输入材料
    mat = r.get("material") or {}
    res = mat.get("resume") or {}
    prior = mat.get("prior_report") or {}
    if res.get("skills") or res.get("projects") or prior:
        parts.append("<h2>本次面试的输入材料</h2><ul>")
        if res.get("source"):
            n = res.get("chars")
            parts.append(f"<li>简历来源：{e(str(res['source']))}"
                         + (f"（{e(str(n))} 字）" if n else "") + "</li>")
        if res.get("years"):
            years_txt = f"{res['years']:g}"
            parts.append(f"<li>识别年限：约 {e(years_txt)} 年</li>")
        if res.get("skills"):
            parts.append(f"<li>识别技能：{e('、'.join(map(str, res['skills'])))}</li>")
        if res.get("projects"):
            parts.append(f"<li>识别项目：{e('；'.join(map(str, res['projects'])))}</li>")
        if res.get("llm_summary"):
            parts.append(f"<li>模型摘要：{e(str(res['llm_summary']))}</li>")
        if prior:
            bits = []
            if prior.get("overall") is not None:
                bits.append(f"综合分 {_pct(prior['overall'])}")
            if prior.get("insufficient_dims"):
                bits.append("上轮未测出：" + "、".join(map(str, prior["insufficient_dims"])))
            parts.append(f"<li>接上一轮：{e('；'.join(bits) or '（已接入）')}</li>")
        parts.append("</ul>")

    parts.append("<h2>模型调用与成本</h2>")
    parts.append(f"<p>调用 {e(str(cost.get('calls') or 0))} 次，"
                 f"合计 {e(_money(cost.get('total_cny')))}。</p>")
    by_model = cost.get("by_model") or {}
    if by_model:
        parts.append("<table><thead><tr><th>模型</th><th>费用</th></tr></thead><tbody>")
        for m, v in by_model.items():
            cny = v.get("cny") if isinstance(v, dict) else v
            parts.append(f"<tr><td>{e(str(m))}</td>"
                         f"<td class=\"num\">{e(_money(cny))}</td></tr>")
        parts.append("</tbody></table>")
    by_purpose = cost.get("by_purpose") or {}
    if by_purpose:
        parts.append("<table><thead><tr><th>用途</th><th>费用</th></tr></thead><tbody>")
        for p, v in by_purpose.items():
            cny = v.get("cny") if isinstance(v, dict) else v
            parts.append(f"<tr><td>{e(str(p))}</td>"
                         f"<td class=\"num\">{e(_money(cny))}</td></tr>")
        parts.append("</tbody></table>")
    unpriced = cost.get("unpriced_models") or []
    if unpriced:
        parts.append(f"<div class=\"note\">未登记价目、按 0 计：{e('、'.join(map(str, unpriced)))}</div>")
    budget = r.get("budget") or {}
    if budget.get("limit_cny") is not None:
        parts.append(f"<p>预算上限 {e(_money(budget.get('limit_cny')))}，"
                     f"已花 {e(_money(budget.get('spent_cny')))}</p>")
        if budget.get("tripped"):
            parts.append(f"<div class=\"note\">本场有 {e(str(budget['tripped']))} 次调用因超出预算"
                         "被降级为规则化实现：面试未中断，但那几轮题目/分析不是模型给的。</div>")

    pacing = r.get("pacing")
    hints = pacing.get("hints") if isinstance(pacing, dict) else None
    if hints:
        parts.append("<h2>节奏建议</h2><ul>")
        for h in hints:
            parts.append(f"<li>{e(str(h))}</li>")
        parts.append("</ul>")
    elif isinstance(pacing, str) and pacing.strip():
        parts.append(f"<h2>节奏建议</h2><p>{e(pacing.strip())}</p>")

    parts.append("<h2>这份报告不做什么</h2><ul>")
    for x in r.get("limitations") or []:
        parts.append(f"<li>{e(str(x))}</li>")
    parts.append("</ul>")

    parts.append("<p class=\"meta\">维度定义见 app/config.py 的 DIMENSIONS；"
                 "分数全部由 app/scoring/engine.py 的规则算出，模型只负责抽取证据。</p>")
    parts.append("</body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
EXPORTERS = {
    "json": (to_json, "application/json; charset=utf-8", "json"),
    "md": (to_markdown, "text/markdown; charset=utf-8", "md"),
    "html": (to_html, "text/html; charset=utf-8", "html"),
}


def export(report: dict, fmt: str = "json") -> tuple[str, str, str]:
    """返回 (正文, content-type, 扩展名)。未知格式退回 json 而不是报错。

    为什么退回而不是 4xx：导出按钮就在报告页上，为了一个格式参数把用户挡在
    门外没有意义；而且 `format=md` 这种拼错的场景，给一份 JSON 也仍然可读。
    """
    fn, ctype, ext = EXPORTERS.get((fmt or "json").lower(), EXPORTERS["json"])
    return fn(report), ctype, ext


def filename(report: dict, fmt: str = "json") -> str:
    name = _candidate_name(report).strip() or "candidate"
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "candidate"
    ext = EXPORTERS.get((fmt or "json").lower(), EXPORTERS["json"])[2]
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return f"interview_report_{safe}_{stamp}.{ext}"


def dimension_reference() -> list[dict]:
    """给文档/前端复用的维度口径表（单一事实来源是 config.DIMENSIONS）。"""
    out = []
    for k, d in DIMENSION_MAP.items():
        out.append({"key": k, "name": d["name"], "weight": d["weight"],
                    "definition": d.get("definition", "")})
    return out
