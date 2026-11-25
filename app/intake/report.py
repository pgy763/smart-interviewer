"""历史报告接入：把上一轮的面试报告读回来，作为下一轮的输入。

需求原文「简历、报告等接入 demo」里的"报告"就是这一层。它要回答的问题是：
**上一轮里哪些维度没测出来、哪些风险信号还没排除**，这一轮要针对性补。

为什么不让模型直接读上一份报告写段总结：
1. 报告是本系统自己生成的结构化 JSON（见 report/builder.py），已经是字段，
   再让模型转述一遍只会引入失真；
2. "哪些维度证据不足"是确定的集合运算，不需要智能；
3. 模型唯一有价值的输入是"建议追问点"，那部分由规则从 timeline 里挑，
   比让模型自由发挥更可控。

所以这一层是**纯结构化吸收**，不调模型。模型的机会在出题阶段（见
providers/llm.py 的 PROMPT_QUESTION，会把上一轮缺口写进去）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# 上一轮报告的哪些字段是我们真正要用的。其余（时间线全文、逐条证据）
# 不进 prompt：几百行 JSON 会把上下文塞满，而信息密度很低。
MAX_CARRY_MESSAGES = 8


@dataclass
class PriorReport:
    """从上一轮报告里抽出来的、这一轮真正要用的东西。"""

    source_id: str = ""                      # 上一轮的 interview_id
    overall: float | None = None
    verdict: str = ""
    scored_dims: dict[str, float] = field(default_factory=dict)      # 已出分维度
    insufficient_dims: list[str] = field(default_factory=list)       # 证据不足维度
    risk_level: str = "low"
    risk_reasons: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)               # 上轮的异常兜底
    probe_points: list[str] = field(default_factory=list)            # 建议本轮重点问
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "overall": self.overall,
            "verdict": self.verdict,
            "scored_dims": self.scored_dims,
            "insufficient_dims": self.insufficient_dims,
            "risk_level": self.risk_level,
            "risk_reasons": self.risk_reasons,
            "fallbacks": self.fallbacks,
            "probe_points": self.probe_points,
            "warnings": self.warnings,
        }

    def prompt_text(self, limit: int = 600) -> str:
        """给 prompt 的紧凑文本。只写"谁的、缺什么、要问什么"。"""
        parts: list[str] = []
        if self.overall is not None:
            parts.append(f"上一轮综合分 {self.overall:g}")
        if self.scored_dims:
            top = sorted(self.scored_dims.items(), key=lambda kv: -kv[1])[:3]
            parts.append("较好：" + "、".join(f"{k}{v:g}" for k, v in top))
        if self.insufficient_dims:
            parts.append("上轮没测出来：" + "、".join(self.insufficient_dims))
        if self.risk_level in ("warn", "high"):
            parts.append(f"上轮风险信号 {self.risk_level}：" + "；".join(self.risk_reasons[:2]))
        if self.probe_points:
            parts.append("建议本轮重点问：" + "；".join(self.probe_points[:3]))
        return "。".join(p for p in parts if p)[:limit]


def _dim_name(key: str) -> str:
    """报告里存的是中文名，也兼容存 key 的情况。"""
    return str(key)


def build_prior_report(report: dict) -> PriorReport:
    """从 report/builder.build_report() 的产物里抽字段。

    设计成能吃**半成品**报告：字段缺失就用默认值，不抛异常。
    理由：上一轮报告可能是老版本生成的（字段少），也可能是异常终止的
    （overall 为 None）。"读不了上一轮"不该阻塞这一轮。
    """
    pr = PriorReport()
    if not isinstance(report, dict):
        pr.warnings.append("上一轮报告格式无法识别")
        return pr

    iv = report.get("interview") or {}
    pr.source_id = iv.get("id") or report.get("interview_id") or ""

    overall = report.get("overall")
    if isinstance(overall, (int, float)):
        pr.overall = float(overall)
    elif overall is None:
        pr.warnings.append("上一轮没有综合分（可能是证据不足）")

    v = report.get("verdict") or {}
    pr.verdict = v.get("kind") or ""

    for d in report.get("dimensions") or []:
        if not isinstance(d, dict):
            continue
        name = _dim_name(d.get("name") or d.get("key") or "")
        if not name:
            continue
        if d.get("status") == "scored" and isinstance(d.get("score"), (int, float)):
            pr.scored_dims[name] = float(d["score"])
        elif d.get("status") == "insufficient_evidence" and name not in pr.insufficient_dims:
            pr.insufficient_dims.append(name)

    risk = report.get("risk") or {}
    pr.risk_level = risk.get("level") or "low"
    pr.risk_reasons = [str(r)[:120] for r in (risk.get("reasons") or [])][:4]

    for f in report.get("fallback_summary") or []:
        if isinstance(f, dict) and f.get("title"):
            pr.fallbacks.append(str(f["title"]))

    pr.probe_points = _derive_probe_points(report, pr)
    return pr


def _derive_probe_points(report: dict, pr: PriorReport) -> list[str]:
    """从上一轮时间线里挑"值得再问一轮"的点。

    规则（都可解释，不调模型）：
    - 上轮答过但 key_points 为 0 的题 → 那题等于没答明白，值得换个角度再问
    - 上轮被追问过的题 → 追问说明第一次没答到位
    最多 3 条，每条写成一句可执行的话。
    """
    out: list[str] = []
    tl = report.get("timeline") or []
    for t in tl:
        if not isinstance(t, dict):
            continue
        q = (t.get("question") or "").strip()
        if not q:
            continue
        short = q[:40] + ("…" if len(q) > 40 else "")
        if t.get("answered") and not (t.get("key_points") or []):
            out.append(f"再问一轮：{short}（上轮没有形成有效论点）")
        elif t.get("phase") == "followup" and t.get("answered") is False:
            out.append(f"补问：{short}（上轮的追问没有回答完）")
        if len(out) >= 3:
            break
    if not out and pr.insufficient_dims:
        out.append("上轮证据不足的维度换成更具体的问题再问一次："
                   + "、".join(pr.insufficient_dims[:3]))
    return out


def parse_report_json(raw: str | bytes | dict) -> dict:
    """解析用户粘贴/上传的上一轮报告。

    接受三种形态：
    - `/api/interview/{id}/report` 返回的 `{"ok":true,"report":{...}}`
    - 直接的报告对象 `{...}` 或完整导出的 JSON 文件
    - 已经解析好的 dict（前端可能直接把 report 对象塞进 body，而不是字符串）

    第三种看着多余，但真实接口里两种都会出现：前端有时 `JSON.stringify` 一次，
    有时直接把对象放进请求体。接口应当同时接受，而不是让调用方去猜。
    """
    if isinstance(raw, dict):
        data = raw
    else:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as e:
            raise ValueError(f"上一轮报告不是合法 JSON：{e}") from e
    if isinstance(data, dict) and isinstance(data.get("report"), dict):
        return data["report"]
    if isinstance(data, dict) and ("dimensions" in data or "overall" in data):
        return data
    raise ValueError("这个 JSON 里找不到报告结构（需要 dimensions 或 overall 字段）")
