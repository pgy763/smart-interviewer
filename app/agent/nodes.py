"""LangGraph 节点实现。

每个节点都是 `(state, runtime) -> dict` 的纯函数，返回**状态增量**。
这一点很重要：LangGraph 会把返回的 dict 合并进 state，如果你直接 mutate state
或者返回整个 state，行为不正确而且不会报错——这是最容易踩的坑。

节点清单（对应"固定环节"）：
    prepare   →  算本轮规划（还剩几题、该不该插压力题/变更题）
    generate  →  出题（调模型）
    analyze   →  分析回答（调模型抽证据）
    decide    →  决定下一步（追问 / 压力 / 变更 / 下一题 / 收尾）
    closing   →  收尾语

为什么"决定下一步"要单独成节点而不是写在条件边里：
条件边里做业务逻辑（比如"如果追问次数少于 2 且发现缺口就追问"）会让
路由函数变重，而且没法把决策记录进 progress。单独成节点后，
决策本身是可观测的（前端能看到"为什么又追问了一轮"）。
"""

from __future__ import annotations

import time

from app.config import DIMENSION_MAP, DIMENSIONS
from app.agent.state import TurnState, InterviewRuntime

# 一轮里最多推进多少步，防止图跑飞
MAX_STEPS = 8


def _ctx(runtime) -> dict:
    """取 context。

    LangGraph 1.x 把第二个参数从 dict 改成了 `Runtime` 对象，
    真正传进去的 context 挂在 `runtime.context` 上。这里做一层兼容：
    既支持 `Runtime`（1.x），也支持裸 dict（0.x 以及单测里直接调用节点）。
    这个坑是实际踩到的——`TypeError: 'Runtime' object is not subscriptable`。
    """
    ctx = getattr(runtime, "context", None)
    if ctx is None and isinstance(runtime, dict):
        ctx = runtime
    return ctx or {}


def _progress(state: TurnState, step: str, detail: str, ok: bool = True) -> list[dict]:
    """progress 是 append 语义：返回**新增的那一条**组成的列表。

    LangGraph 对 list 字段默认是覆盖，这里图省事用 `progress` 的增量拼接
    在调用方完成（见 graph.py 的 _merge_progress）。所以节点只返回一条。
    这个取舍写在 docs/DESIGN.md 里——不用 Annotated[list, operator.add]，
    因为本项目里"整场进度"由会话层保管，图内只需要返回本轮的新增。
    """
    return [{"step": step, "detail": detail, "ok": ok, "ts": time.time()}]


# ---------------------------------------------------------------------------
# 1. prepare：规划
# ---------------------------------------------------------------------------
def node_prepare(state: TurnState, runtime: InterviewRuntime) -> dict:
    """根据已完成的轮次，算本轮该做什么、考察哪个维度。

    规划逻辑（全部可解释）：
    1. 统计已用过的主问题/追问/压力/变更轮次
    2. 若某个维度还没有任何证据，优先补它（保证覆盖）
    3. 压力题和变更题各最多一次，且只在"还有余量"时插入
    4. 剩余轮次不够了就收尾
    """
    cfg = _ctx(runtime)["config"]
    turns = state.get("turns") or []
    target_total = cfg.questions_target

    phase_counts: dict[str, int] = {}
    for t in turns:
        phase_counts[t.get("phase", "main")] = phase_counts.get(t.get("phase", "main"), 0) + 1

    main_done = phase_counts.get("main", 0)
    has_pressure = phase_counts.get("pressure", 0) > 0
    has_variant = phase_counts.get("variant", 0) > 0
    remaining_main = max(target_total - main_done, 0)

    # 维度覆盖：哪些维度还没有任何轮次指向它
    covered = {t.get("question_kind") for t in turns if t.get("question_kind")}
    enabled = cfg.enabled_dimensions
    uncovered = [k for k in enabled if k not in covered]

    # 选目标维度：优先未覆盖的，按配置顺序；都覆盖了就轮换
    if uncovered:
        target_dim = uncovered[0]
    else:
        target_dim = enabled[main_done % len(enabled)]

    # 压力题插在多一半的时候，这样前面已有足够答案可对比
    want_pressure = (
        cfg.pressure_round and not has_pressure
        and "stress" in enabled
        and main_done >= max(2, target_total // 2)
    )
    want_variant = (
        cfg.variant_round and not has_variant
        and "adaptability" in enabled
        and main_done >= max(2, target_total - 1)
    )

    plan = {
        "main_done": main_done,
        "remaining_main": remaining_main,
        "has_pressure": has_pressure,
        "has_variant": has_variant,
        "want_pressure": want_pressure,
        "want_variant": want_variant,
        "uncovered_dims": uncovered,
        "target_total": target_total,
    }
    return {
        "plan": plan,
        "target_dim": target_dim,
        "progress": _progress(
            state, "prepare",
            f"已答主问题 {main_done}/{target_total}；本轮考察「{DIMENSION_MAP[target_dim]['name']}」"
            + (f"；还有 {len(uncovered)} 个维度未覆盖" if uncovered else ""),
        ),
    }


# ---------------------------------------------------------------------------
# 2. generate：出题
# ---------------------------------------------------------------------------
def node_generate(state: TurnState, runtime: InterviewRuntime) -> dict:
    """调模型出下一道题。模型挂了就用规则题库兜底，绝不让面试卡住。"""
    provider = _ctx(runtime)["provider"]
    cfg = _ctx(runtime)["config"]
    plan = state.get("plan") or {}

    # 已经问过的问题：避免重复
    asked = list(state.get("asked_questions") or [])
    for t in state.get("turns") or []:
        if t.get("question"):
            asked.append(t["question"])

    target_dim = state.get("target_dim") or cfg.enabled_dimensions[0]
    dim_name = DIMENSION_MAP[target_dim]["name"]

    res = provider.question({
        "interview_id": state.get("interview_id"),
        "job_role": state.get("job_role") or cfg.job_role,
        "target_dim": dim_name,
        "difficulty": state.get("difficulty") or cfg.difficulty,
        "asked": asked[-8:],
        "resume": state.get("resume") or "",
    })

    q = (res.data or {}).get("question") or "请介绍一下你最近做的一个项目，以及你在其中负责的部分。"
    why = (res.data or {}).get("why") or ""

    ok = bool(res.ok and (res.data or {}).get("question"))
    detail = f"出题：{q[:40]}…" if len(q) > 40 else f"出题：{q}"
    if not ok and res.error:
        detail += f"（模型异常已降级：{res.error[:40]}）"

    return {
        "question": q,
        "next_question": q,
        "next_action": "ask",
        "progress": _progress(state, "generate", detail, ok=ok),
    }


# ---------------------------------------------------------------------------
# 3. analyze：分析回答
# ---------------------------------------------------------------------------
def node_analyze(state: TurnState, runtime: InterviewRuntime) -> dict:
    """抽取回答里的客观证据。这是评分的数据来源。"""
    provider = _ctx(runtime)["provider"]
    answer = (state.get("answer") or "").strip()
    if not answer:
        return {
            "analysis": {"empty": True},
            "progress": _progress(state, "analyze", "候选人不作答（空回答）", ok=False),
        }

    res = provider.analyze_answer({
        "interview_id": state.get("interview_id"),
        "question": state.get("question") or "",
        "answer": answer,
    })
    data = res.data or {}
    data.setdefault("answer", answer)
    data["phase"] = state.get("phase") or "main"

    kp = len(data.get("key_points") or [])
    detail = (f"抽取证据：{kp} 个论点，"
              f"{'有' if data.get('has_number') else '无'}数字，"
              f"{'有' if data.get('has_example') else '无'}举例，"
              f"回避词 {len(data.get('hedges') or [])} 处")
    return {
        "analysis": data,
        "progress": _progress(state, "analyze", detail),
    }


# ---------------------------------------------------------------------------
# 4. decide：决定下一步
# ---------------------------------------------------------------------------
def node_decide(state: TurnState, runtime: InterviewRuntime) -> dict:
    """本轮结束后的路由决策。这是"agent 负责追问决策与节奏"的核心。

    决策优先级（从高到低）：
    1. 空回答 → 追问一次确认（防止网络问题被当成不答）
    2. 回答有明确缺口 && 追问次数未超 → 追问
    3. 该插压力题 && 有余量 → 压力
    4. 该插变更题 && 有余量 → 变更
    5. 还有剩余主问题 → 下一题
    6. 否则 → 收尾
    """
    cfg = _ctx(runtime)["config"]
    provider = _ctx(runtime)["provider"]
    plan = state.get("plan") or {}
    turns = state.get("turns") or []
    phase = state.get("phase") or "main"
    answer = (state.get("answer") or "").strip()
    analysis = state.get("analysis") or {}

    # 本轮已追问过几轮
    idx = state.get("idx", 0)
    followups_this_question = sum(
        1 for t in turns
        if t.get("phase") == "followup" and t.get("parent_idx") == state.get("parent_idx")
    )

    # 需要"是否追问"的判断：只有当本阶段是 main 或 followup 且还有追问余量才问模型
    if phase in ("main", "followup") and followups_this_question < cfg.followup_max:
        if not answer:
            return {
                "next_action": "followup",
                "next_question": "抱歉，刚才没有收到你的回答。是网络问题吗？能再说一遍吗？",
                "followup_decision": {"should_followup": True, "gap": "空回答",
                                      "reason": "空回答先确认是否技术问题"},
                "progress": _progress(state, "decide", "空回答 → 追问确认是否网络问题", ok=False),
            }

        res = provider.followup({
            "interview_id": state.get("interview_id"),
            "question": state.get("question") or "",
            "answer": answer,
        })
        fd = res.data or {}
        if fd.get("should_followup") and (fd.get("followup") or "").strip():
            return {
                "next_action": "followup",
                "next_question": fd["followup"].strip(),
                "followup_decision": fd,
                "progress": _progress(
                    state, "decide",
                    f"发现缺口「{fd.get('gap') or '未标注'}」→ 追问：{fd.get('reason') or ''}"),
            }
        # 不追问，记录决策供报告使用
        decision_ok = {"should_followup": False, "gap": "", "reason": fd.get("reason") or ""}
    else:
        decision_ok = {"should_followup": False, "gap": "", "reason": "追问次数已达上限"}

    # 压力题
    if plan.get("want_pressure") and phase != "pressure":
        res = provider.pressure({
            "interview_id": state.get("interview_id"),
            "answer": answer,
        })
        q = (res.data or {}).get("question") or "你这个结论我持怀疑态度，你怎么说服我？"
        return {
            "next_action": "pressure",
            "next_question": q,
            "target_dim": "stress",
            "followup_decision": decision_ok,
            "progress": _progress(state, "decide", f"插入压力题（考察抗压）：{q[:30]}"),
        }

    # 变更题
    if plan.get("want_variant") and phase != "variant":
        res = provider.variant({
            "interview_id": state.get("interview_id"),
            "question": state.get("question") or "",
            "answer": answer,
        })
        q = (res.data or {}).get("question") or "如果量级放大一百倍，你刚才说的方案还成立吗？"
        changed = (res.data or {}).get("changed") or "条件变更"
        return {
            "next_action": "variant",
            "next_question": q,
            "target_dim": "adaptability",
            "followup_decision": decision_ok,
            "progress": _progress(state, "decide", f"插入条件变更题（{changed}）"),
        }

    # 还有主问题
    if (plan.get("remaining_main") or 0) > 0:
        return {
            "next_action": "ask",
            "followup_decision": decision_ok,
            "progress": _progress(
                state, "decide",
                f"本轮回合结束，继续下一题（还剩 {plan.get('remaining_main')} 题）"),
        }

    # 收尾
    return {
        "next_action": "closing",
        "next_question": "今天的面试就到这里，感谢你的时间。你有什么想问我的吗？",
        "followup_decision": decision_ok,
        "progress": _progress(state, "decide", "目标题量已完成 → 收尾"),
    }


# ---------------------------------------------------------------------------
# 5. closing
# ---------------------------------------------------------------------------
def node_closing(state: TurnState, runtime: InterviewRuntime) -> dict:
    """收尾。刻意不让模型生成收尾语——这类话术模型容易过度承诺
    （"我们会尽快通知你"），而本项目不做任何录用判断，收尾必须是固定话术。"""
    return {
        "next_action": "end",
        "next_question": "",
        "progress": _progress(state, "closing", "面试结束，进入报告生成"),
    }
