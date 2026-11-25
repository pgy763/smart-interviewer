"""评分引擎：规则算分，模型只供证据。

**这是整个项目最该被问的一块，先说清楚为什么不"让模型打个分"**

让 qwen-max 直接输出 {"confidence": 78} 看起来很省事，但有四个硬问题：

1. **不可复现**：同一段回答，温度 0.3 下两次调用可能差 10 分。面试官问"为什么是 78
   不是 72"，你答不上来。
2. **不可审计**：分数背后没有任何可核对的数字。候选人申诉时无法自证。
3. **系统性偏差**：模型会受用词华丽程度、回答长度影响，把"会说话"当成"能力强"。
4. **无法表达"证据不足"**：模型倾向于给一个中间值（70 左右），看起来合理，
   实际上是"我不知道"被伪装成"中等"。

所以这里的结构是：
   模型/启发式  →  抽取客观证据（有没有数字、有没有举例、回避词几个、语速变异系数）
   规则          →  把证据映射成分数，且规则可读、可测试、可解释
   阈值          →  证据条数不够就判 insufficient_evidence，不给分

每个维度的评分函数都返回 Evidence 列表，报告里逐条展示——
"为什么给 82 分"必须能落到具体的 3 条证据上。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import DIMENSION_MAP, DIMENSIONS


@dataclass
class Evidence:
    """一条可展示给面试官的证据。"""
    kind: str          # text | audio | video | agent
    text: str          # 人话描述
    value: float | None = None   # 对应的数值（可选）
    turn_idx: int | None = None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text,
                "value": self.value, "turn_idx": self.turn_idx}


@dataclass
class DimScore:
    key: str
    name: str
    score: float | None            # None = 证据不足
    status: str                    # scored | insufficient_evidence
    reason: str
    evidence: list[Evidence] = field(default_factory=list)
    weight: float = 0.0

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "score": self.score,
            "status": self.status, "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
            "weight": self.weight,
        }


# 每维度至少需要多少条证据才允许出分。低于这个数就是"抽不到"，不给分。
MIN_EVIDENCE = 3


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# ---------------------------------------------------------------------------
# 单维度评分
# ---------------------------------------------------------------------------
def score_confidence(ctx: dict) -> DimScore:
    """自信：回避词密度 + 语速稳定性 + 视线稳定度。

    刻意**不用**"语气是否强硬"这类主观信号——本地拿不到可靠的韵律特征，
    硬编会造成"声音大的人分高"。用可量化的三样。
    """
    ev: list[Evidence] = []
    parts: list[float] = []

    analyses = ctx["answer_analyses"]
    if analyses:
        # 回避词：每百字出现次数，>3 视为明显不自信
        hedge_rates = []
        for a in analyses:
            text = a.get("answer") or ""
            hedges = a.get("hedges") or []
            if text:
                hedge_rates.append(len(hedges) / max(len(text), 1) * 100)
        if hedge_rates:
            rate = _avg(hedge_rates)
            # 0 处 → 90 分；每 1%/百字 掉 25 分，下限 30
            s = _clamp(90 - rate * 25, 30, 95)
            parts.append(s)
            ev.append(Evidence("text", f"回避性措辞密度 {rate:.1f} 处/百字（越低越笃定）",
                               round(rate, 2)))

    audio = ctx["audio"]
    if audio.get("speed_cv") is not None:
        from app.audio.analyzer import speed_stability_score
        s, why = speed_stability_score(audio)
        if s is not None:
            parts.append(s)
            ev.append(Evidence("audio", why, s))

    vision = ctx["vision"]
    if vision.get("looking_ratio") is not None:
        lr = vision["looking_ratio"]
        # 视线稳定度：这是"自信"的弱信号，权重靠后，所以只贡献一条证据，
        # 不单独决定分数（放在最后取均值时会被其它两项稀释）
        s = _clamp(40 + lr * 55, 40, 95)
        parts.append(s)
        ev.append(Evidence("video", f"视线朝向镜头占比 {lr:.0%}", round(lr, 4)))

    if len(ev) < MIN_EVIDENCE:
        return DimScore("confidence", "自信", None, "insufficient_evidence",
                        f"只收集到 {len(ev)} 条证据，不足以判断（需要 ≥{MIN_EVIDENCE}）",
                        ev, DIMENSION_MAP["confidence"]["weight"])
    score = _avg(parts)
    return DimScore("confidence", "自信", round(score, 1), "scored",
                    _label(score, DIMENSION_MAP["confidence"]["scale"]), ev,
                    DIMENSION_MAP["confidence"]["weight"])


def score_comprehension(ctx: dict) -> DimScore:
    """理解：答到点上 + 没跑题 + 追问命中。

    跑题是最硬的负面信号：一次跑题直接压到 55 以下。
    """
    ev: list[Evidence] = []
    analyses = ctx["answer_analyses"]
    if not analyses:
        return DimScore("comprehension", "理解", None, "insufficient_evidence",
                        "没有可分析的问答", [], DIMENSION_MAP["comprehension"]["weight"])

    off = sum(1 for a in analyses if a.get("off_topic"))
    total = len(analyses)
    ev.append(Evidence("text", f"{total} 个回答中 {off} 个明显跑题", off))
    base = 88 - (off / max(total, 1)) * 50

    # 关键论点数量：有论点的回答说明真听懂了
    kp_counts = [len(a.get("key_points") or []) for a in analyses]
    avg_kp = _avg([float(x) for x in kp_counts])
    ev.append(Evidence("text", f"平均每个回答给出 {avg_kp:.1f} 个关键论点", round(avg_kp, 2)))
    if avg_kp < 1.0:
        base -= 15
    elif avg_kp >= 2.5:
        base += 4

    # agent 追问里的"理解缺口"：因为没听懂而追问，算负面
    gaps = [g for g in ctx.get("followup_gaps", []) if g]
    misunderstanding = sum(1 for g in gaps if g in ("跳跃", "矛盾"))
    if misunderstanding:
        base -= misunderstanding * 6
        ev.append(Evidence("agent", f"{misunderstanding} 次追问暴露逻辑跳跃或自相矛盾",
                           misunderstanding))
    else:
        ev.append(Evidence("agent", "追问未暴露逻辑跳跃或矛盾", 0))

    if len(ev) < MIN_EVIDENCE:
        return DimScore("comprehension", "理解", None, "insufficient_evidence",
                        "证据不足", ev, DIMENSION_MAP["comprehension"]["weight"])
    score = _clamp(base)
    return DimScore("comprehension", "理解", round(score, 1), "scored",
                    _label(score, DIMENSION_MAP["comprehension"]["scale"]), ev,
                    DIMENSION_MAP["comprehension"]["weight"])


def score_communication(ctx: dict) -> DimScore:
    """沟通：结构连接词 + 句长 + 填充词。"""
    ev: list[Evidence] = []
    parts: list[float] = []
    analyses = ctx["answer_analyses"]
    answered = [a for a in analyses if (a.get("answer") or "").strip()]
    if not answered:
        return DimScore("communication", "沟通", None, "insufficient_evidence",
                        "没有可分析的回答", [], DIMENSION_MAP["communication"]["weight"])

    # 1) 结构连接词：每个回答平均出现几个
    marker_counts = [len(a.get("structure_markers") or []) for a in answered]
    avg_marker = _avg([float(x) for x in marker_counts])
    s1 = _clamp(45 + avg_marker * 16, 35, 92)
    parts.append(s1)
    ev.append(Evidence("text", f"平均每答 {avg_marker:.1f} 个结构连接词（首先/因为/所以…）",
                       round(avg_marker, 2)))

    # 2) 句长：太短说明讲不清，太长说明不分层。20-45 字是舒适区
    lens = []
    for a in answered:
        text = a["answer"]
        n_sent = max(a.get("sentences") or 1, 1)
        lens.append(len(text) / n_sent)
    avg_len = _avg(lens)
    if 20 <= avg_len <= 45:
        s2 = 82.0
    elif avg_len < 20:
        s2 = _clamp(82 - (20 - avg_len) * 1.5, 45, 82)
    else:
        s2 = _clamp(82 - (avg_len - 45) * 0.8, 50, 82)
    parts.append(s2)
    ev.append(Evidence("text", f"平均句长 {avg_len:.1f} 字（20–45 字为易懂区间）",
                       round(avg_len, 1)))

    # 3) 填充词
    filler_rates = []
    for a in answered:
        text = a["answer"]
        fillers = a.get("fillers") if a.get("fillers") is not None else []
        if text:
            filler_rates.append(len(fillers) / max(len(text), 1) * 100)
    if filler_rates:
        fr = _avg(filler_rates)
        s3 = _clamp(88 - fr * 14, 40, 92)
        parts.append(s3)
        ev.append(Evidence("audio", f"填充词密度 {fr:.1f} 处/百字（嗯/那个/就是）", round(fr, 2)))

    score = _avg(parts)
    return DimScore("communication", "沟通", round(score, 1), "scored",
                    _label(score, DIMENSION_MAP["communication"]["scale"]), ev,
                    DIMENSION_MAP["communication"]["weight"])


def score_execution(ctx: dict) -> DimScore:
    """执行：第一人称动作 + 可量化结果。这两样是"真的做过"的最强信号。"""
    ev: list[Evidence] = []
    analyses = ctx["answer_analyses"]
    answered = [a for a in analyses if (a.get("answer") or "").strip()]
    if not answered:
        return DimScore("execution", "执行", None, "insufficient_evidence",
                        "没有可分析的回答", [], DIMENSION_MAP["execution"]["weight"])

    has_num = sum(1 for a in answered if a.get("has_number"))
    has_ex = sum(1 for a in answered if a.get("has_example"))
    n = len(answered)

    num_ratio = has_num / n
    ex_ratio = has_ex / n
    ev.append(Evidence("text", f"{n} 个回答中 {has_num} 个含具体数字/规模", round(num_ratio, 3)))
    ev.append(Evidence("text", f"{n} 个回答中 {has_ex} 个举了具体项目/例子", round(ex_ratio, 3)))

    # 第一人称动作词：区分"我们做了"和"我做了"。这是学生简历最常见的稀释点。
    action_hits = 0
    for a in answered:
        text = a["answer"]
        action_hits += sum(text.count(w) for w in DIMENSION_MAP["execution"]["action_words"])
    action_avg = action_hits / n
    ev.append(Evidence("text", f"平均每答 {action_avg:.1f} 处第一人称动作（我实现/我压测…）",
                       round(action_avg, 2)))

    if len(ev) < MIN_EVIDENCE:
        return DimScore("execution", "执行", None, "insufficient_evidence",
                        "证据不足", ev, DIMENSION_MAP["execution"]["weight"])

    base = 40 + num_ratio * 30 + ex_ratio * 20 + min(action_avg, 3) * 4
    score = _clamp(base)
    return DimScore("execution", "执行", round(score, 1), "scored",
                    _label(score, DIMENSION_MAP["execution"]["scale"]), ev,
                    DIMENSION_MAP["execution"]["weight"])


def score_learning(ctx: dict) -> DimScore:
    """学习：只说"看文档"不给分，要说路径+验证方式。"""
    ev: list[Evidence] = []
    analyses = ctx["answer_analyses"]
    answered = [a for a in analyses if (a.get("answer") or "").strip()]
    if not answered:
        return DimScore("learning", "学习", None, "insufficient_evidence",
                        "没有可分析的回答", [], DIMENSION_MAP["learning"]["weight"])

    words = DIMENSION_MAP["learning"]["action_words"]
    hits = 0
    hits_per_answer: list[int] = []
    for a in answered:
        c = sum(a["answer"].count(w) for w in words)
        hits += c
        hits_per_answer.append(c)
    ev.append(Evidence("text", f"共出现 {hits} 处学习动作（查文档/读源码/复现/对比）", hits))

    if hits == 0:
        # 完全没有学习过程描述 → 这一维无法判断，不是低分
        return DimScore("learning", "学习", None, "insufficient_evidence",
                        "全程没有提到任何具体的学习过程（查了什么、怎么验证的），无法评估",
                        ev, DIMENSION_MAP["learning"]["weight"])

    # 有学习动作还得有"怎么验证学会了"
    verify_hits = sum(1 for a in answered
                      if any(w in a["answer"] for w in ("跑通", "复现", "测了", "对比", "验证", "压测")))
    ev.append(Evidence("text", f"{verify_hits} 个回答提到验证方式（跑通/复现/对比/压测）",
                       verify_hits))

    if len(ev) < MIN_EVIDENCE:
        ev.append(Evidence("text", "学习路径描述较笼统，缺可复述细节", None))

    base = 45 + min(hits, 6) * 5 + verify_hits * 6
    score = _clamp(base)
    return DimScore("learning", "学习", round(score, 1), "scored",
                    _label(score, DIMENSION_MAP["learning"]["scale"]), ev,
                    DIMENSION_MAP["learning"]["weight"])


def score_patience(ctx: dict) -> DimScore:
    """耐心：被追问后的回答长度是否塌陷 + 压力段视线是否崩。

    **必须有追问轮次才能评。** 全程没追问过 → 证据不足，不给分。
    这是"不编分"原则最典型的落地。
    """
    ev: list[Evidence] = []
    turns = ctx["turns"]
    if not any(t.get("phase") == "followup" for t in turns):
        return DimScore("patience", "耐心", None, "insufficient_evidence",
                        "本场没有触发追问轮次，无法观察被追问时的稳定性",
                        ev, DIMENSION_MAP["patience"]["weight"])

    # 追问后的回答长度 vs 主问题回答长度
    main_lens = [len(t["answer"]) for t in turns
                 if t.get("phase") == "main" and t.get("answer")]
    fu_lens = [len(t["answer"]) for t in turns
               if t.get("phase") == "followup" and t.get("answer")]
    if not fu_lens or not main_lens:
        return DimScore("patience", "耐心", None, "insufficient_evidence",
                        "追问轮次没有有效回答", ev, DIMENSION_MAP["patience"]["weight"])

    ratio = _avg([float(x) for x in fu_lens]) / max(_avg([float(x) for x in main_lens]), 1)
    ev.append(Evidence("agent", f"追问后回答长度是主回答的 {ratio:.0%}（塌陷说明不耐烦）",
                       round(ratio, 3)))

    # 塌陷到 50% 以下开始扣，越低越狠；超过 120% 说明反而更投入
    if ratio >= 1.0:
        s1 = 85.0
    elif ratio >= 0.5:
        s1 = 60 + (ratio - 0.5) * 50
    else:
        s1 = _clamp(35 + ratio * 50, 30, 60)
    ev.append(Evidence("agent", _label(s1, DIMENSION_MAP["patience"]["scale"]), None))

    # 压力段/追问段的视频稳定性（如果有）
    vision = ctx["vision"]
    if vision.get("away_ratio") is not None:
        away = vision["away_ratio"]
        ev.append(Evidence("video", f"整场离开画面占比 {away:.0%}", round(away, 4)))

    if len(ev) < MIN_EVIDENCE:
        return DimScore("patience", "耐心", None, "insufficient_evidence",
                        "证据条数不足", ev, DIMENSION_MAP["patience"]["weight"])
    score = _clamp(s1)
    return DimScore("patience", "耐心", round(score, 1), "scored",
                    _label(score, DIMENSION_MAP["patience"]["scale"]), ev,
                    DIMENSION_MAP["patience"]["weight"])


def score_stress(ctx: dict) -> DimScore:
    """抗压：压力题前后的答案质量差。

    **必须有压力轮次**，否则不给分。这是设计上的硬约束，不是实现偷懒。
    """
    ev: list[Evidence] = []
    turns = ctx["turns"]
    pressure_turns = [t for t in turns if t.get("phase") == "pressure" and t.get("answer")]
    if not pressure_turns:
        return DimScore("stress", "抗压", None, "insufficient_evidence",
                        "本场未启用压力题（可在参数面板开启），无法评估抗压能力",
                        ev, DIMENSION_MAP["stress"]["weight"])

    before = [a for a in ctx["answer_analyses"] if a.get("phase") == "main"]
    after = [a for a in ctx["answer_analyses"] if a.get("phase") == "pressure"]
    if not before or not after:
        return DimScore("stress", "抗压", None, "insufficient_evidence",
                        "压力段缺少可对比的回答", ev, DIMENSION_MAP["stress"]["weight"])

    def quality(a: dict) -> float:
        """单答案质量代理：有论点 + 有数字 + 没跑题，减掉回避词。"""
        s = len(a.get("key_points") or []) * 8
        s += 12 if a.get("has_number") else 0
        s -= 18 if a.get("off_topic") else 0
        s -= len(a.get("hedges") or []) * 4
        return s

    q_before = _avg([quality(a) for a in before])
    q_after = _avg([quality(a) for a in after])
    drop = q_before - q_after
    ev.append(Evidence("agent", f"压力题前质量分 {q_before:.1f} → 压力后 {q_after:.1f}",
                       round(drop, 2)))

    # 压力段停顿是否变多
    audio = ctx["audio"]
    if audio.get("pause_ratio"):
        pr = audio["pause_ratio"]
        ev.append(Evidence("audio", f"整场停顿占比 {pr:.0%}", round(pr, 4)))
        if pr > 0.5:
            drop += 6

    base = 85 - drop * 1.6
    score = _clamp(base)
    ev.append(Evidence("agent", _label(score, DIMENSION_MAP["stress"]["scale"]), None))
    return DimScore("stress", "抗压", round(score, 1), "scored",
                    _label(score, DIMENSION_MAP["stress"]["scale"]), ev,
                    DIMENSION_MAP["stress"]["weight"])


def score_adaptability(ctx: dict) -> DimScore:
    """适应：条件变更题里是否重构方案而不是复读。"""
    ev: list[Evidence] = []
    turns = ctx["turns"]
    variant = [t for t in turns if t.get("phase") == "variant" and t.get("answer")]
    if not variant:
        return DimScore("adaptability", "适应", None, "insufficient_evidence",
                        "本场未启用条件变更题（可在参数面板开启），无法评估",
                        ev, DIMENSION_MAP["adaptability"]["weight"])

    v_answers = [a for a in ctx["answer_analyses"] if a.get("phase") == "variant"]
    if not v_answers:
        return DimScore("adaptability", "适应", None, "insufficient_evidence",
                        "变更题缺少有效回答", ev, DIMENSION_MAP["adaptability"]["weight"])

    # 信号 1：回答里是否出现"改/换/调整/重构/如果…那么"这类方案调整词
    adjust_words = ("改成", "换成", "调整", "重构", "拆", "分片", "加缓存", "改成异步",
                    "先改", "换成读扩散", "而不是", "区别在于")
    hits = sum(1 for a in v_answers if any(w in a["answer"] for w in adjust_words))
    ev.append(Evidence("agent", f"{hits}/{len(v_answers)} 个变更题回答给出了方案调整",
                       hits))

    # 信号 2：是否只是重复原答案（用与原问题回答的字符重合率近似）
    reuse = _reuse_ratio(turns)
    if reuse is not None:
        ev.append(Evidence("agent", f"与原题回答的文本重合度 {reuse:.0%}（越高越像复读）",
                           round(reuse, 3)))

    if len(ev) < MIN_EVIDENCE:
        ev.append(Evidence("agent", "只收集到 2 条信号", None))

    base = 50 + hits * 15
    if reuse is not None and reuse > 0.6:
        base -= 20
    score = _clamp(base)
    return DimScore("adaptability", "适应", round(score, 1), "scored",
                    _label(score, DIMENSION_MAP["adaptability"]["scale"]), ev,
                    DIMENSION_MAP["adaptability"]["weight"])


def _reuse_ratio(turns: list[dict]) -> float | None:
    """变更题回答与原题回答的重合度（用字符 3-gram 集合的 Jaccard 近似）。"""
    main = {}
    for t in turns:
        if t.get("phase") == "main" and t.get("answer"):
            main[t["idx"]] = t["answer"]
    variants = [t for t in turns if t.get("phase") == "variant" and t.get("answer")]
    if not variants or not main:
        return None
    ratios = []
    for v in variants:
        # 变更题紧跟在某道主问题后面，取 idx 最小差值的那条
        near = min(main.items(), key=lambda kv: abs(kv[0] - v["idx"]))[1]
        a = _ngrams(v["answer"])
        b = _ngrams(near)
        if not a or not b:
            continue
        ratios.append(len(a & b) / len(a | b))
    return _avg(ratios) if ratios else None


def _ngrams(s: str, n: int = 3) -> set[str]:
    s = "".join(s.split())
    return {s[i:i + n] for i in range(max(len(s) - n + 1, 0))}


def _label(score: float, scale: list[tuple[int, str]]) -> str:
    for threshold, text in scale:
        if score >= threshold:
            return text
    return scale[-1][1]


SCORERS = {
    "confidence": score_confidence,
    "comprehension": score_comprehension,
    "communication": score_communication,
    "execution": score_execution,
    "learning": score_learning,
    "patience": score_patience,
    "stress": score_stress,
    "adaptability": score_adaptability,
}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def score_all(ctx: dict, enabled: list[str], weights: dict[str, float]) -> dict:
    """跑全部启用维度，算加权总分。

    **总分的两条规矩**：
    1. 证据不足的维度不进分母（不能把"没测到"当 0 分拖低总分）。
    2. 被启用但全部证据不足时，总分返回 None 并在报告里说明——
       宁可说"这场面试信息量不够"，也不给一个假分数。
    """
    dims: list[DimScore] = []
    for key in enabled:
        fn = SCORERS.get(key)
        if not fn:
            continue
        ds = fn(ctx)
        ds.weight = weights.get(key, 0.0)
        dims.append(ds)

    scored = [d for d in dims if d.status == "scored" and d.score is not None]
    weight_sum = sum(d.weight for d in scored)
    if weight_sum <= 0:
        overall = None
    else:
        overall = sum(d.score * d.weight for d in scored) / weight_sum

    missing = [d.name for d in dims if d.status != "scored"]
    return {
        "dimensions": [d.to_dict() for d in dims],
        "overall": round(overall, 1) if overall is not None else None,
        "scored_count": len(scored),
        "insufficient_count": len(missing),
        "insufficient_names": missing,
        "weights_effective": {d.key: round(d.weight, 4) for d in scored},
    }
