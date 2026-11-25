"""模型通道：一个接口，两种实现。

**为什么要有 mock 通道**
这个 demo 交付出去要能被别人`git clone` 之后直接跑。如果强依赖 API Key，
打开就是一片 500，什么也看不到。所以：
- mock：确定性、零成本、覆盖全部链路（出题/追问/评分/报告/视觉判定）
- dashscope：真实通义千问（qwen-plus 文本 + qwen-vl-max 视觉 + omni 实时语音）

`provider=auto` 时：有 DASHSCOPE_API_KEY 就走真实模型，没有就降级 mock，并在
报告里**明确标注本次是 mock 数据**（见 report/builder.py 的 provider 字段）——
不能让人误以为 mock 分是真实模型评的。

**为什么统一走 JSON 输出**
所有调用都要求模型返回 JSON，且用 `parse_json_lenient` 处理"模型在 JSON 外面
加了 ```json 代码块 / 前后说了客套话"这种常见脏输出。不写这个，线上一定炸。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from app import db
from app.config import PRICING, price_of, settings


# ---------------------------------------------------------------------------
# 统一的返回结构
# ---------------------------------------------------------------------------
@dataclass
class LLMResult:
    ok: bool
    data: dict
    raw: str = ""
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error: str | None = None


def parse_json_lenient(text: str) -> dict:
    """从模型输出里抠出 JSON。

    处理的现实情况（按出现频率排序）：
    1. 纯 JSON                —— 直接 loads
    2. ```json ... ``` 包裹   —— 去围栏
    3. 前面有"好的，以下是…"   —— 找第一个 { 到最后一个 }
    4. 被截断（没闭合）        —— 尝试补齐右括号，失败则返回 {}
    """
    if not text:
        return {}
    s = text.strip()
    # 去 markdown 围栏
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {"value": obj}
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        frag = s[start:end + 1]
        try:
            obj = json.loads(frag)
            return obj if isinstance(obj, dict) else {"value": obj}
        except json.JSONDecodeError:
            # 补右括号抢救被截断的输出
            depth_c = depth_b = 0
            for ch in frag:
                if ch == "{":
                    depth_c += 1
                elif ch == "}":
                    depth_c -= 1
                elif ch == "[":
                    depth_b += 1
                elif ch == "]":
                    depth_b -= 1
            patched = frag + "]" * max(depth_b, 0) + "}" * max(depth_c, 0)
            try:
                obj = json.loads(patched)
                return obj if isinstance(obj, dict) else {"value": obj}
            except json.JSONDecodeError:
                return {}
    return {}


def _hash01(*parts: str) -> float:
    """确定性伪随机：同样输入永远同样输出。mock 通道靠它保证可复现。"""
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


# ---------------------------------------------------------------------------
# 提示词（结构化：角色 / 任务 / 约束 / 输出 schema 四段式）
# 面试官要求"提示词工程结构化"，这就是结构化。
# ---------------------------------------------------------------------------
PROMPT_QUESTION = """你是{company_role}的资深技术面试官，正在面试一位{job_role}候选人。

# 任务
根据候选人简历与已完成的问答，出**下一道**面试题。

# 约束
1. 只出一道题，不要附带答案、不要解释你的思路。
2. 题目必须能考察「{target_dim}」这个维度。
3. 难度：{difficulty}。
4. 不要重复已经问过的问题，也不要问简历里没有的东西。
5. 问题要像真人说话，一句话到两句话，不超过 80 字。
6. 如果简历信息不足，就问一个该岗位的通用基础题。

# 已问过
{asked}

# 候选人简历摘要
{resume}
{prior_hint}
# 输出（严格 JSON，不要其它文字）
{{"question": "题目文本", "target_dim": "考察维度key", "why": "一句话说明这题想看出什么"}}
"""

PROMPT_FOLLOWUP = """你是资深技术面试官。候选人对下面这个问题做了回答，你要决定是否追问、以及追问什么。

# 原问题
{question}

# 候选人回答
{answer}

# 约束
1. 只有当回答里存在「含糊、缺证据、逻辑跳跃、与自己前后矛盾」时才追问。
2. 追问要具体，指到那句含糊的话，不要泛泛问"能详细说说吗"。
3. 追问不超过 60 字。
4. 若回答已经具体充分，则 should_followup 为 false。

# 输出（严格 JSON）
{{"should_followup": true, "followup": "追问文本", "reason": "为什么追问", "gap": "缺口类型(含糊/缺证据/矛盾/跳跃)"}}
"""

PROMPT_PRESSURE = """你是资深技术面试官，现在要给候选人一点压力，观察其稳定性。

# 候选人刚才说
{answer}

# 约束
1. 用质疑或反问的方式问一句，针对他说的某个具体结论，不要人身攻击。
2. 不超过 50 字。
3. 只输出一句问话。

# 输出（严格 JSON）
{{"question": "压力问句", "target": "你质疑的具体点"}}
"""

PROMPT_VARIANT = """你是资深技术面试官，要考察候选人的临场适应能力。

# 原问题
{question}

# 候选人回答
{answer}

# 任务
把原问题的某个条件改掉（比如规模放大 100 倍、换一个约束、去掉某个前提），
再问一次，看他是重构方案还是硬套旧答案。

# 约束
1. 必须先明确说出改了哪个条件，再提问。
2. 不超过 80 字。

# 输出（严格 JSON）
{{"question": "变更后的问句", "changed": "改了什么条件"}}
"""

PROMPT_ANALYZE_ANSWER = """你是面试评估助手。给你一道题和候选人的回答，请抽取**客观证据**，不要给总分。

# 题目
{question}

# 回答
{answer}

# 要抽取的字段
- key_points: 回答里出现的关键论点（数组，每条不超过 15 字，最多 5 条）
- has_example: 是否举了具体例子或项目（布尔）
- has_number: 是否出现可验证的数字/规模/时间（布尔）
- hedges: 出现的回避性措辞（数组，如"可能""大概"）
- structure_markers: 出现的结构连接词（数组，如"首先""然后"）
- off_topic: 是否明显答非所问（布尔）
- quality_note: 一句话客观描述，不评价人

# 输出（严格 JSON）
{{"key_points": [], "has_example": false, "has_number": false, "hedges": [],
  "structure_markers": [], "off_topic": false, "quality_note": ""}}
"""

PROMPT_VISION = """你在分析一张在线面试的摄像头画面（可能包含候选人的脸）。

# 任务
判断这些客观事实，不要评价长相、不要推测情绪。

# 输出（严格 JSON）
{{"face_found": true, "looking_at_camera": true, "head_pose": "front|left|right|down|up",
  "note": "一句话客观描述"}}
"""


# ---------------------------------------------------------------------------
# Mock 实现
# ---------------------------------------------------------------------------
PROMPT_RESUME_DIGEST = """你在帮面试官准备一场技术面试。下面是候选人的简历摘要。

# 任务
1. 用一句话概括这份简历的技术方向与项目密度（不超过 60 字）。
2. 给出 3 个**值得追问**的点，每个一行，不超过 40 字。

# 约束
- 不要评价候选人好坏，不要给分数或录用建议。
- 不要复述简历里的原句。
- 追问点必须指向简历里**有提到但没展开**的地方（比如只写了用了 Redis 没写怎么用）。
- 如果简历信息不足，就给该岗位的通用追问点。

# 简历摘要
{resume}

# 输出（严格 JSON，不要其它文字）
{{"summary": "一句话概括", "probe_points": ["追问点1", "追问点2", "追问点3"]}}
"""

# 接入层 → prompt 的两个适配函数
#
# 为什么不在节点里拼字符串：prompt 的拼接规则属于 provider 的职责边界，
# 换一个 provider（比如 OpenAI）时它有自己的 prompt 习惯，改一处就够。
# 这两条都必须**永不抛异常**——简历解析结果可能缺字段，而"少一个字段"
# 绝不该让整场面试出不了题。
# ---------------------------------------------------------------------------
def _resume_for_prompt(ctx: dict) -> str:
    """优先用结构化档案拼摘要，退化到原文截断。

    结构化档案（技能 + 项目标题 + 两条要点）比原文更省 token、信息密度更高；
    但简历解析失败时档案是空的，这时必须能用原文兜底，否则整场面试会退化成
    通用题——那是最差的结果，比多花几百 token 差得多。
    """
    prof = ctx.get("resume_profile") or {}
    if isinstance(prof, dict) and (prof.get("skills") or prof.get("projects")):
        parts = []
        if prof.get("skills"):
            parts.append("技能：" + "、".join(str(s) for s in prof["skills"][:18]))
        for p in (prof.get("projects") or [])[:4]:
            if not isinstance(p, dict):
                continue
            head = str(p.get("title") or "项目")[:60]
            bullets = [str(b)[:80] for b in (p.get("bullets") or [])[:2] if b]
            parts.append(head + ("；" + "；".join(bullets) if bullets else ""))
        if prof.get("years"):
            parts.append(f"经验约 {prof['years']} 年")
        if prof.get("llm_summary"):
            parts.append("画像：" + str(prof["llm_summary"])[:200])
        if parts:
            return "\n".join(parts)[:1200]
    return (ctx.get("resume") or "（无）")[:1200]


def _prior_hint(prior: dict) -> str:
    """上一轮报告的缺口提示。空 dict 时返回空串（prompt 里就是空行）。"""
    if not isinstance(prior, dict) or not prior:
        return ""
    bits = []
    if prior.get("overall") is not None:
        bits.append(f"上一轮综合分 {prior['overall']}")
    if prior.get("insufficient_dims"):
        bits.append("上一轮这些维度证据不足：" + "、".join(str(x) for x in prior["insufficient_dims"][:5]))
    if prior.get("risk_level") in ("warn", "high"):
        bits.append(f"上一轮风险信号 {prior['risk_level']}")
    for p in (prior.get("probe_points") or [])[:2]:
        bits.append(f"建议重点问：{p}")
    if not bits:
        return ""
    return "\n# 上一轮面试（同一候选人）\n" + "；".join(bits) + "\n" + \
        "本轮要优先把上一轮没测出来的维度问出来，但不要重复上一轮的原题。\n"


# ---------------------------------------------------------------------------
QUESTION_BANK = {
    "concept": [
        "你说你做过即时通讯系统，消息的「不丢不重不乱序」具体是怎么保证的？",
        "Redis 的 INCR 发号和数据库自增发号，在高并发下有什么区别？",
        "介绍一下你对 TCP 粘包半包的理解，以及你会怎么处理。",
        "如果一个接口 P99 突然从 200ms 涨到 2s，你会按什么顺序排查？",
        "你说用了写扩散，那如果是 5000 人的大群，这个方案还成立吗？",
        "幂等键过期了但请求还在重试，会发生什么？你怎么防？",
    ],
    "project": [
        "你简历里这个项目，最难的一个技术决定是什么？当时还有别的选项吗？",
        "这个项目上线过吗？如果没有，你怎么证明它是对的？",
        "你压测到 250 msg/s，那瓶颈在哪？如果再翻十倍你先改哪里？",
        "你说做了 159 项断言，举一个真实抓到的 bug，以及你是怎么定位的。",
    ],
    "behavior": [
        "讲一次你和别人技术意见不一致、最后你被说服的经历。",
        "有没有哪次交付你不得不砍功能？砍的依据是什么？",
    ],
}


class MockProvider:
    """确定性实现：同样的输入永远同样的输出，方便写断言、方便演示复现。"""

    name = "mock"

    def question(self, ctx: dict) -> LLMResult:
        started = time.time()
        role = ctx.get("job_role", "")
        target = ctx.get("target_dim", "comprehension")
        asked = ctx.get("asked", [])

        # 规则化实现也要吃「材料」——否则演示时"上传简历/接上一轮报告"这件事
        # 在 mock 通道下完全看不出效果，用户会以为功能没做。
        prior = ctx.get("prior_report") or {}
        probe = [p for p in (prior.get("probe_points") or [])
                 if p and p not in asked]
        if probe:
            pick = probe[0]
            data = {
                "question": pick,
                "target_dim": target,
                "why": "上一轮这场面试的同类维度没测出来，这次换成更具体的问题再问一次。",
            }
            return LLMResult(True, data, json.dumps(data, ensure_ascii=False),
                             settings.text_model, self.name, 0, 0,
                             int((time.time() - started) * 1000))

        projects = ((ctx.get("resume_profile") or {}).get("projects") or [])
        titles = [p.get("title") for p in projects if p.get("title")]
        # 只在第一道题用简历里的项目名，避免整场都在问同一个项目
        if titles and not asked:
            pick = f"简历里写了「{titles[0]}」，挑一个最难的模块讲清楚：它解决什么问题、你具体做了什么、怎么验证的。"
            data = {
                "question": pick,
                "target_dim": target,
                "why": "从简历里真实存在的项目切入，比通用题库更能看出候选人是否真的做过。",
            }
            return LLMResult(True, data, json.dumps(data, ensure_ascii=False),
                             settings.text_model, self.name, 0, 0,
                             int((time.time() - started) * 1000))

        pool: list[str] = []
        for group in QUESTION_BANK.values():
            pool.extend(group)
        # 按 target_dim + 已问过 做确定性挑选
        candidates = [q for q in pool if q not in asked]
        if not candidates:
            candidates = pool
        pick = candidates[int(_hash01(role, target, str(len(asked))) * len(candidates)) % len(candidates)]
        data = {
            "question": pick,
            "target_dim": target,
            "why": f"考察{target}，用一道需要给出机制细节的题，看他是否只背结论。",
        }
        return LLMResult(True, data, json.dumps(data, ensure_ascii=False),
                         settings.text_model, self.name, 0, 0,
                         int((time.time() - started) * 1000))

    def followup(self, ctx: dict) -> LLMResult:
        answer = (ctx.get("answer") or "").strip()
        # 规则化判断：短回答 / 有回避词 / 无数字 → 追问
        hedges = [w for w in ("可能", "大概", "应该", "好像", "差不多", "不太确定") if w in answer]
        has_number = any(ch.isdigit() for ch in answer)
        short = len(answer) < 40
        should = bool(hedges or short or not has_number)
        if should:
            if hedges:
                fu = f"你刚才用了「{hedges[0]}」这个词，具体是哪里不确定？给个明确结论。"
                reason, gap = "回答里出现回避措辞", "含糊"
            elif not has_number:
                fu = "你讲的都是定性的，能给一个具体的数字或者规模吗？"
                reason, gap = "缺少可验证数据", "缺证据"
            else:
                fu = "这段回答偏概括，能不能就其中一个点说到实现细节？"
                reason, gap = "回答过短", "缺证据"
        else:
            fu, reason, gap = "", "回答已给出机制与数据", ""
        data = {"should_followup": should, "followup": fu, "reason": reason, "gap": gap}
        return LLMResult(True, data, json.dumps(data, ensure_ascii=False),
                         settings.text_model, self.name)

    def pressure(self, ctx: dict) -> LLMResult:
        answer = (ctx.get("answer") or "")
        # 从回答里挑一个最长的名词性片段当质疑对象，保证"针对具体结论"
        frag = ""
        for token in re.split(r"[，。；、\s]+", answer):
            if len(token) >= 4:
                frag = token
                break
        target = frag or "你说的这个方案"
        data = {"question": f"你说{target}，可是我觉得这个方案在生产环境根本跑不起来，你怎么解释？",
                "target": target}
        return LLMResult(True, data, json.dumps(data, ensure_ascii=False),
                         settings.text_model, self.name)

    def variant(self, ctx: dict) -> LLMResult:
        q = ctx.get("question") or "这个方案"
        data = {"question": f"如果量级突然放大一百倍，你刚才说的{q[-20:]}这套方案还成立吗？你会先改哪一块？",
                "changed": "量级放大 100 倍"}
        return LLMResult(True, data, json.dumps(data, ensure_ascii=False),
                         settings.text_model, self.name)

    def analyze_answer(self, ctx: dict) -> LLMResult:
        answer = ctx.get("answer") or ""
        hedges = [w for w in ("可能", "大概", "应该", "好像", "差不多", "不太确定", "记不清") if w in answer]
        markers = [w for w in ("首先", "其次", "然后", "最后", "第一", "第二", "因为", "所以", "总结", "比如") if w in answer]
        has_number = bool(re.search(r"\d", answer))
        has_example = any(w in answer for w in ("项目", "当时", "我们线上", "我做过", "举个例子", "比如"))
        # 关键论点：取最长的几个分句
        clauses = [c.strip() for c in re.split(r"[。；！？\n]+", answer) if len(c.strip()) >= 8]
        clauses.sort(key=len, reverse=True)
        data = {
            "key_points": [c[:15] for c in clauses[:5]],
            "has_example": has_example,
            "has_number": has_number,
            "hedges": hedges,
            "structure_markers": markers,
            "off_topic": len(answer.strip()) < 6,
            "quality_note": f"回答 {len(answer)} 字，{'有' if has_number else '无'}具体数字，"
                            f"{'有' if has_example else '无'}举例，回避词 {len(hedges)} 处。" ,
        }
        return LLMResult(True, data, json.dumps(data, ensure_ascii=False),
                         settings.text_model, self.name)

    def vision(self, ctx: dict) -> LLMResult:
        # mock 阶段不做真实图像推理，返回"未知"，由本地启发式 CV 主导
        data = {"face_found": None, "looking_at_camera": None,
                "head_pose": "unknown", "note": "mock 通道不做视觉推理"}
        return LLMResult(True, data, json.dumps(data, ensure_ascii=False),
                         settings.vision_model, self.name)

    def resume_digest(self, ctx: dict) -> LLMResult:
        """简历增强：一句话画像 + 3 个值得追问的点。

        mock 版不"理解"简历，只按规则给：拿技能数量与项目数量说明概况，
        再用项目标题生成追问点。这样离线演示时这块也有内容，不会空着。
        """
        text = (ctx.get("resume") or "").strip()
        heads = [ln.strip() for ln in text.splitlines() if ln.strip()]
        n_skill = len([h for h in heads if "技能" in h])
        projects = [h for h in heads if h.startswith("项目：")]
        summary = f"材料约 {len(text)} 字，识别到 {len(projects)} 个可追问的项目条目。"
        points = [f"{p[3:][:40]} 里的技术选型理由" for p in projects[:3]]
        if not points:
            points = ["最近一个项目里最难的技术决定", "有没有上线、怎么验证的", "如果重做会改哪里"]
        data = {"summary": summary, "probe_points": points}
        return LLMResult(True, data, json.dumps(data, ensure_ascii=False),
                         settings.text_model, self.name)


# ---------------------------------------------------------------------------
# DashScope 实现
# ---------------------------------------------------------------------------
class DashScopeProvider:
    """真实模型。每条调用都记进 llm_call 表，供成本核算。"""

    name = "dashscope"

    def __init__(self, api_key: str):
        self.api_key = api_key
        try:
            import dashscope
            from dashscope import Generation, MultiModalConversation
            dashscope.api_key = api_key
            self._gen = Generation
            self._mm = MultiModalConversation
            self._ok = True
        except Exception as e:  # pragma: no cover - 环境缺依赖时才走
            self._ok = False
            self._err = str(e)

    # ---- 内部：一次文本调用 ----
    def _chat(self, purpose: str, prompt: str, interview_id: str | None,
              model: str | None = None) -> LLMResult:
        model = model or settings.text_model
        started = time.time()
        if not self._ok:  # pragma: no cover
            return LLMResult(False, {}, model=model, provider=self.name,
                             error=getattr(self, "_err", "dashscope 不可用"))
        try:
            resp = self._gen.call(
                model=model,
                prompt=prompt,
                result_format="message",
                # 低温度：面试题的确定性比创意重要
                parameters={"temperature": 0.3, "result_format": "message"},
            )
            latency = int((time.time() - started) * 1000)
            if resp.status_code != 200:
                err = f"{resp.status_code}: {getattr(resp, 'message', '')}"
                db.log_llm_call(interview_id, self.name, model, purpose, 0, 0, 0, latency, False, err)
                return LLMResult(False, {}, model=model, provider=self.name,
                                 latency_ms=latency, error=err)
            content = resp.output.choices[0].message.content
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            usage = getattr(resp, "usage", None) or {}
            it = int(usage.get("input_tokens", 0) or 0)
            ot = int(usage.get("output_tokens", 0) or 0)
            db.log_llm_call(interview_id, self.name, model, purpose, it, ot, 0, latency, True, None)
            return LLMResult(True, parse_json_lenient(content), content, model, self.name,
                             it, ot, latency)
        except Exception as e:  # pragma: no cover
            latency = int((time.time() - started) * 1000)
            db.log_llm_call(interview_id, self.name, model, purpose, 0, 0, 0, latency, False, str(e))
            return LLMResult(False, {}, model=model, provider=self.name,
                             latency_ms=latency, error=str(e))

    def question(self, ctx: dict) -> LLMResult:
        prompt = PROMPT_QUESTION.format(
            company_role="一家做 AI 应用的公司",
            job_role=ctx.get("job_role", ""),
            target_dim=ctx.get("target_dim", "理解"),
            difficulty=ctx.get("difficulty", "medium"),
            asked="\n".join(f"- {q}" for q in ctx.get("asked", [])) or "（还没问）",
            resume=_resume_for_prompt(ctx),
            prior_hint=_prior_hint(ctx.get("prior_report") or {}),
        )
        r = self._chat("question", prompt, ctx.get("interview_id"))
        if r.ok and r.data.get("question"):
            return r
        # 模型返回脏数据时退回规则库，保证面试不会卡住
        fallback = MockProvider().question(ctx)
        fallback.error = r.error or "模型输出缺少 question 字段，已降级规则题库"
        fallback.model = r.model
        return fallback

    def followup(self, ctx: dict) -> LLMResult:
        prompt = PROMPT_FOLLOWUP.format(question=ctx.get("question", ""), answer=ctx.get("answer", ""))
        r = self._chat("followup", prompt, ctx.get("interview_id"))
        if r.ok and "should_followup" in r.data:
            return r
        fb = MockProvider().followup(ctx)
        fb.error = r.error
        return fb

    def pressure(self, ctx: dict) -> LLMResult:
        prompt = PROMPT_PRESSURE.format(answer=ctx.get("answer", "")[:600])
        r = self._chat("question", prompt, ctx.get("interview_id"))
        return r if r.ok and r.data.get("question") else MockProvider().pressure(ctx)

    def variant(self, ctx: dict) -> LLMResult:
        prompt = PROMPT_VARIANT.format(question=ctx.get("question", ""), answer=ctx.get("answer", "")[:600])
        r = self._chat("question", prompt, ctx.get("interview_id"))
        return r if r.ok and r.data.get("question") else MockProvider().variant(ctx)

    def analyze_answer(self, ctx: dict) -> LLMResult:
        prompt = PROMPT_ANALYZE_ANSWER.format(question=ctx.get("question", ""), answer=ctx.get("answer", ""))
        r = self._chat("score", prompt, ctx.get("interview_id"))
        if r.ok and r.data:
            return r
        fb = MockProvider().analyze_answer(ctx)
        fb.error = r.error
        return fb

    def vision(self, ctx: dict) -> LLMResult:
        """Qwen-VL 判定单帧。图片以 data URI 传入。"""
        started = time.time()
        b64 = ctx.get("image_b64") or ""
        if not b64 or not self._ok:
            return MockProvider().vision(ctx)
        try:
            messages = [{
                "role": "user",
                "content": [
                    {"image": f"data:image/jpeg;base64,{b64}"},
                    {"text": PROMPT_VISION},
                ],
            }]
            resp = self._mm.call(model=settings.vision_model, messages=messages)
            latency = int((time.time() - started) * 1000)
            if resp.status_code != 200:
                db.log_llm_call(ctx.get("interview_id"), self.name, settings.vision_model,
                                "vision_judge", 0, 0, 0, latency, False, str(resp.message))
                return MockProvider().vision(ctx)
            content = resp.output.choices[0].message.content
            text = "".join(p.get("text", "") for p in content if isinstance(p, dict)) \
                if isinstance(content, list) else str(content)
            usage = getattr(resp, "usage", None) or {}
            db.log_llm_call(ctx.get("interview_id"), self.name, settings.vision_model,
                            "vision_judge", int(usage.get("input_tokens", 0) or 0),
                            int(usage.get("output_tokens", 0) or 0), 0, latency, True, None)
            data = parse_json_lenient(text)
            if not data:
                return MockProvider().vision(ctx)
            return LLMResult(True, data, text, settings.vision_model, self.name, 0, 0, latency)
        except Exception as e:  # pragma: no cover
            return MockProvider().vision(ctx)


    def resume_digest(self, ctx: dict) -> LLMResult:
        """用真实模型读简历摘要，产出"一句话画像 + 3 个值得追问的点"。

        约束刻意写得紧：不许他复述简历、不许他评价候选人，只许提"还能问什么"。
        原因是这份输出会进 prompt 和报告首页，一旦写成"该候选人技术扎实"，
        报告就变成了模型在给结论——那违反了本项目"分数由规则算"的边界。
        """
        prompt = PROMPT_RESUME_DIGEST.format(resume=(ctx.get("resume") or "（无）")[:1200])
        r = self._chat("resume_digest", prompt, ctx.get("interview_id"))
        if r.ok and isinstance(r.data, dict):
            return r
        fb = MockProvider().resume_digest(ctx)
        fb.error = r.error or "模型输出不符合 schema，已降级规则摘要"
        fb.model = r.model
        return fb


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
_provider = None


def get_provider():
    global _provider
    if _provider is not None:
        return _provider
    kind = settings.resolved_provider()
    if kind == "dashscope" and settings.dashscope_api_key:
        _provider = DashScopeProvider(settings.dashscope_api_key)
    else:
        _provider = MockProvider()
    return _provider


def reset_provider() -> None:
    """测试用：清掉单例，让下次按新配置重新构造。"""
    global _provider
    _provider = None


def estimate_cost(calls: list[dict]) -> dict:
    """把 llm_call 账本换算成钱。成本核算是验收项之一，必须能算给面试官看。

    价目用 `price_of()` 按前缀匹配：模型名是部署时用环境变量换的，
    换成一个没登记在 PRICING 里的模型时，成本按 0 计，而不是让整场面试
    在最后一步算钱时抛 KeyError。
    """
    total = 0.0
    by_purpose: dict[str, float] = {}
    by_model: dict[str, float] = {}
    unpriced: set[str] = set()
    for c in calls:
        price = price_of(c["model"])
        if c["model"] not in PRICING:
            unpriced.add(c["model"])
        cost = 0.0
        if "per_minute" in price:
            cost += price["per_minute"] * (c.get("audio_ms") or 0) / 60000.0
        cost += price.get("input_per_1k", 0.0) * (c.get("input_tokens") or 0) / 1000.0
        cost += price.get("output_per_1k", 0.0) * (c.get("output_tokens") or 0) / 1000.0
        total += cost
        by_purpose[c["purpose"]] = by_purpose.get(c["purpose"], 0.0) + cost
        by_model[c["model"]] = by_model.get(c["model"], 0.0) + cost
    return {
        "total_cny": round(total, 6),
        "by_purpose": {k: round(v, 6) for k, v in by_purpose.items()},
        "by_model": {k: round(v, 6) for k, v in by_model.items()},
        "calls": len(calls),
        # 有调用但价目表里没有的模型：报告里要标出来，否则「成本 0」是假的
        "unpriced_models": sorted(unpriced),
    }
