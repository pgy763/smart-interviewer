"""报告生成。

报告是这套系统的唯一"交付物"——需求里验收标准就是"能导出含分数的分析报告"。
所以这里的设计原则是：**宁可少给分，不可给假分**。

三件事必须出现在报告里：
1. 分数 + 每个分数的证据（不能只有数字）
2. 风险信号（读稿/离席）与它的局限
3. 成本账单（调了多少次模型、多少钱）——这也是验收项之一

**为什么不直接调模型生成一段总结**：模型写总结会顺手编数字、编百分比，
而这份报告是要给面试官当决策参考的。本项目让模型只做"抽取证据"这一件事，
所有加总、加权、分档都在代码里算——这样每一分都能追到哪句话。
这是整套系统最重要的一条判断，写进 docs/DESIGN.md。
"""

from __future__ import annotations

import json
import time
from typing import Any

from app import db
from app.audio.analyzer import pause_features, text_features
from app.config import DIMENSION_MAP, InterviewConfig
from app.providers.llm import estimate_cost
from app.scoring.engine import score_all
from app.vision.analyzer import script_risk_score, summarize_frames


def build_context(iid: str) -> dict:
    """把库里的原始事件整理成评分引擎要的 ctx。

    这一步是"数据 → 评分"的唯一入口，评分函数都是纯粹对 ctx 的运算，
    所以只要把 ctx 造对，评分就可以被单测覆盖（见 tests/test_scoring.py）。
    """
    turns = db.list_turns(iid)
    frames = db.list_vision_frames(iid)
    audios = db.list_audio_events(iid)

    # 每一轮的证据：从 audio_event 里找该轮对应的分析结果
    # 简化做法：analyze 的结果在提交答案时已经落进 llm ledger 不了，
    # 所以这里用确定性重算——对每个非空回答重新跑一次文本分析。
    # 为什么不复用图里的 analysis：图是"当时"的模型输出，报告要能重放；
    # 重算保证"同样的答案永远同样的证据"，也让报告不依赖历史调用是否成功。
    from app.providers.llm import MockProvider

    prov = MockProvider()  # 只用它的 analyze_answer（规则化，不花钱）
    analyses = []
    for t in turns:
        ans = (t.get("answer") or "").strip()
        if not ans:
            continue
        r = prov.analyze_answer({"answer": ans})
        d = r.data or {}
        d["answer"] = ans
        d["phase"] = t.get("phase") or "main"
        d["turn_idx"] = t.get("idx")
        d["question"] = t.get("question")
        analyses.append(d)

    vision = summarize_frames(frames)
    audio = pause_features(turns, audios)
    # 合并所有回答的文本特征
    all_text = "\n".join((t.get("answer") or "") for t in turns if t.get("answer"))
    tf = text_features(all_text)
    audio["formal_density"] = round(
        sum(1 for a in analyses if a.get("structure_markers")) / max(len(analyses), 1), 3)

    followup_gaps = []
    # 从 turn 的 phase 顺序推：紧跟 followup 的那条 main 的 gap 无法还原，
    # 所以退化成"出现过追问就记为一次命中"，只用于 comprehension 的轻量提示。
    for t in turns:
        if t.get("phase") == "followup":
            followup_gaps.append("追问")

    return {
        "turns": turns,
        "analyses": analyses,
        "answer_analyses": analyses,
        "vision": vision,
        "audio": audio,
        "text": tf,
        "followup_gaps": followup_gaps,
    }


def build_report(iid: str) -> dict:
    """生成完整报告（dict）。HTML 模板直接吃这个结构。"""
    from app.api.session import detect_fallbacks, manager

    row = db.get_interview(iid)
    if not row:
        raise KeyError(f"面试不存在: {iid}")

    try:
        cfg_dict = json.loads(row.get("config_json") or "{}")
    except Exception:
        cfg_dict = {}
    allowed = {f for f in InterviewConfig.__dataclass_fields__}
    cfg = InterviewConfig(**{k: v for k, v in cfg_dict.items() if k in allowed})

    ctx = build_context(iid)
    enabled = cfg.enabled_dimensions or list(DIMENSION_MAP)
    weights = cfg.effective_weights()
    scored = score_all(ctx, enabled, weights)

    fb = detect_fallbacks(iid)

    # 风险信号：读稿/离席
    risk, risk_reasons = script_risk_score(ctx["vision"], ctx["audio"])

    # 成本
    calls = db.list_llm_calls(iid)
    cost = estimate_cost(calls)

    # 预算熔断：这一场有没有被保险丝降级过。
    # 熔断信息必须进报告——否则客户看到"成本 ¥0.4"会以为我们省着花，
    # 实际是后面几轮用了规则化实现，质量与成本都变过。
    session_obj = manager.get(iid)
    budget = session_obj.guard.to_dict() if session_obj else {
        "limit_cny": None, "spent_cny": None, "tripped": 0, "exhausted": False,
        "note": "进程已重启，本场熔断计数不可得",
    }

    # 接入层产物：这场面试是带着什么材料跑的。
    # 报告要能回答"你凭这份简历出的题"，否则面试官没法复盘题目是否对路。
    mat = db.get_material(iid)
    rp = mat.get("resume_profile") or {}
    pr = mat.get("prior_report") or {}
    material = {
        "resume": {
            "source": (mat.get("material") or {}).get("resume", {}).get("source") or "",
            "chars": rp.get("raw_chars"),
            "skills": rp.get("skills") or [],
            "projects": [p.get("title") for p in (rp.get("projects") or [])][:5],
            "years": rp.get("years"),
            "llm_summary": rp.get("llm_summary") or "",
            "warnings": rp.get("warnings") or [],
        },
        "prior_report": {
            "source_id": pr.get("source_id"),
            "overall": pr.get("overall"),
            "insufficient_dims": pr.get("insufficient_dims") or [],
            "probe_points": pr.get("probe_points") or [],
        } if pr else {},
    }

    # 落库维度分（方便列表页直接读）
    db.save_dimension_scores(iid, scored["dimensions"])

    overall = scored["overall"]
    # 兜底优先级：没进度 / 没音频 → 不给总分
    if fb["flags"].get("no_progress") or (fb["flags"].get("empty_audio") and scored["scored_count"] == 0):
        verdict = "insufficient_data"
        verdict_text = "这场面试没有产生可评估的数据，不出具分数。"
    elif overall is None:
        verdict = "insufficient_data"
        verdict_text = "所有维度都证据不足，不出具总分（宁可说信息量不够，也不给假分数）。"
    elif fb["flags"].get("candidate_left") or fb["flags"].get("disconnected"):
        verdict = "partial"
        verdict_text = "已出分，但过程中检测到异常（见下方信号），分数仅供参考。"
    else:
        verdict = "ok"
        verdict_text = "正常完成。"

    # 时长控制建议（是否该延长/缩短）
    s = manager.get(iid)
    if s and fb["duration_sec"] > cfg.max_minutes * 60:
        pacing = f"实际时长 {fb['duration_sec'] / 60:.1f} 分钟，已超过上限 {cfg.max_minutes} 分钟。"
    elif fb["duration_sec"] < cfg.base_minutes * 60 * 0.5:
        pacing = f"实际时长 {fb['duration_sec'] / 60:.1f} 分钟，明显短于基准 {cfg.base_minutes} 分钟。"
    else:
        pacing = f"实际时长 {fb['duration_sec'] / 60:.1f} 分钟，在基准 {cfg.base_minutes} 分钟附近。"

    return {
        "interview": {
            "id": iid,
            "candidate": row.get("candidate_name"),
            "job_role": row.get("job_role"),
            "status": row.get("status"),
            "provider": row.get("provider"),
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
            "duration_sec": fb["duration_sec"],
        },
        "config": {
            "base_minutes": cfg.base_minutes,
            "max_minutes": cfg.max_minutes,
            "questions_target": cfg.questions_target,
            "followup_max": cfg.followup_max,
            "difficulty": cfg.difficulty,
            "enabled": enabled,
            "weights": {k: round(v, 4) for k, v in weights.items()},
        },
        "verdict": {"kind": verdict, "text": verdict_text},
        "overall": overall,
        "overall_label": _overall_label(overall),
        "dimensions": scored["dimensions"],
        "scored_count": scored["scored_count"],
        "insufficient_names": scored["insufficient_names"],
        "weights_effective": scored["weights_effective"],
        "fallbacks": fb["flags"],
        "fallback_summary": _fallback_summary(fb["flags"]),
        "risk": {
            "score": round(risk, 3),
            "level": _risk_level(risk),
            "reasons": risk_reasons,
            "vision": ctx["vision"],
            "audio_headline": {
                "speed_chars_per_sec": ctx["audio"].get("speed_chars_per_sec"),
                "speed_cv": ctx["audio"].get("speed_cv"),
                "pause_ratio": ctx["audio"].get("pause_ratio"),
                "filler_ratio": ctx["text"].get("filler_ratio"),
            },
        },
        "timeline": _timeline(ctx["turns"], ctx["analyses"]),
        "pacing": pacing,
        "cost": cost,
        # 预算熔断与材料来源：这两块让报告能自证"我们是怎么花的钱、凭的什么材料"
        "budget": budget,
        "material": material,
        "provider": row.get("provider"),
        "limitations": LIMITATIONS,
        "generated_at": time.time(),
    }


LIMITATIONS = [
    "面部/肢体判断基于本地启发式视觉（肤色连通域 + 帧间差异），不做人脸识别与情绪识别；"
    "它只能回答『大概在看哪、有没有人』，不能回答『他在想什么』。",
    "『是否看镜头』受摄像头角度、光线影响较大，因此只作为风险提示，不直接扣能力分。",
    "读稿风险是间接推断（视线持续偏离 + 语速平稳 + 书面语密度高），存在误判可能；"
    "报告里从不把它写成定论。",
    "8 个维度里，耐心/抗压/适应三个维度必须有对应环节（追问/压力题/条件变更题）"
    "才可能出分，否则标记为『证据不足』，不进总分分母。",
    "所有分数由代码规则计算，模型只负责抽取证据。"
    "这意味着分数可复算、可追溯，但也意味着它不会比规则本身更聪明。",
    "本 demo 不做录用与否的判断，报告只作为面试官的辅助观察记录。",
]


def _overall_label(v: float | None) -> str:
    if v is None:
        return "无法评估"
    for th, label in [(85, "明显高于平均水平"), (70, "良好"), (55, "中等偏上"),
                      (40, "中等偏下"), (0, "偏弱")]:
        if v >= th:
            return label
    return "偏弱"


def _risk_level(v: float) -> str:
    if v >= 0.7:
        return "high"
    if v >= 0.45:
        return "warn"
    return "low"


def _fallback_summary(flags: dict) -> list[dict]:
    """把技术性的 flag 翻译成能直接放在报告顶部的一句话。"""
    out = []
    mapping = {
        "no_progress": ("没有产生有效问答", "可能创建后未开始，或全程未作答。"),
        "empty_audio": ("没有采集到任何回答文本", "麦克风未授权或未开录音。"),
        "candidate_left": ("检测到中途离开画面", "详见风险信号里的离席时长。"),
        "no_video": ("没有收到任何视频帧", "摄像头未授权或浏览器不支持。"),
        "disconnected": ("会话中断", "长时间无新事件，可从断点继续。"),
        "aborted": ("会话被主动中止", "见中止原因。"),
        "graph_error": ("推理链路出现过异常", "已自动降级，记录在此供排查。"),
    }
    for k, v in flags.items():
        title, desc = mapping.get(k, (k, ""))
        detail = v if isinstance(v, str) else (v.get("note") if isinstance(v, dict) else "")
        out.append({"code": k, "title": title, "desc": desc, "detail": detail})
    return out


def _timeline(turns: list[dict], analyses: list[dict]) -> list[dict]:
    """逐轮摘要，报告里给"这场面试到底问了什么"用。"""
    by_idx = {a.get("turn_idx"): a for a in analyses}
    out = []
    for t in turns:
        a = by_idx.get(t.get("idx")) or {}
        out.append({
            "idx": t.get("idx"),
            "phase": t.get("phase"),
            "phase_label": {"main": "主问题", "followup": "追问",
                            "pressure": "压力题", "variant": "条件变更"}.get(t.get("phase"), t.get("phase")),
            "question": t.get("question"),
            "answer": t.get("answer") or "",
            "answered": bool((t.get("answer") or "").strip()),
            "key_points": a.get("key_points") or [],
            "has_number": a.get("has_number"),
            "has_example": a.get("has_example"),
            "off_topic": a.get("off_topic"),
            "latency_ms": t.get("latency_ms"),
        })
    return out
