"""全局配置：能力维度注册表、权重、判定阈值、模型定价、运行时设置。

三条设计取舍（面试要能讲）：

1. 维度/权重/阈值全部写在代码里，不做成数据库表。
   这是"产品口径"——面试官要能看懂、能改。放在代码里可以用 git diff 审阅变更，
   也能被单元测试直接断言。真正需要按租户定制时再迁库，demo 阶段不提前抽象。

2. 阈值是"判定规则"，不是"让模型自评一个分"。
   模型只负责抽证据（说了什么、有没有停顿、有没有看镜头），分数由规则算出来。
   理由见 docs/DESIGN.md「为什么分数不能由模型直接给」。

3. 每个维度都必须有"证据不足"的出口。
   单场 45 分钟里，抗压/适应力这类维度经常抽不到证据。抽不到就打
   insufficient_evidence，宁可不出分，也不编一个分。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "app" / "static"
TEMPLATE_DIR = BASE_DIR / "app" / "templates"

DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 能力维度注册表
# ---------------------------------------------------------------------------
# 每项：key / 中文名 / 权重 / 一句话定义 / 证据来源 / 判定档位
# 证据来源说明这一维到底靠什么打分，避免"看起来很多维度其实全靠模型编"。
DIMENSIONS: list[dict] = [
    {
        "key": "confidence",
        "name": "自信",
        "weight": 0.14,
        "definition": "表达是否笃定，遇到追问是否动摇，是否频繁使用不确定措辞。",
        "evidence": ["文本：不确定词密度", "音频：语速稳定性", "视频：视线稳定度"],
        "scale": [
            (80, "表达笃定，被追问后仍能给出明确结论"),
            (60, "整体确定，个别处用『可能/大概』回避"),
            (40, "多处自降确定性，被追问即改口"),
            (0, "几乎全程回避正面回答"),
        ],
        "hedges": ["可能", "大概", "也许", "应该是", "不太确定", "好像", "记不清", "差不多"],
    },
    {
        "key": "comprehension",
        "name": "理解",
        "weight": 0.16,
        "definition": "是否听懂问题、是否答到点上、是否主动澄清歧义。",
        "evidence": ["文本：与题目的关键词重合度", "文本：是否跑题", "agent：追问命中率"],
        "scale": [
            (80, "准确抓到问题核心，回答与题目强相关"),
            (60, "方向对，但有部分答偏"),
            (40, "需要追问两轮才理解题意"),
            (0, "基本答非所问"),
        ],
    },
    {
        "key": "communication",
        "name": "沟通",
        "weight": 0.14,
        "definition": "结构是否清楚、是否有条理、是否能让听者跟上。",
        "evidence": ["文本：结构连接词密度", "文本：平均句长与段落数", "音频：填充词比例"],
        "scale": [
            (80, "先给结论再展开，分层清楚"),
            (60, "能听懂，结构一般"),
            (40, "想到哪说到哪，需要听者自己拼"),
            (0, "表述零散，无法跟上"),
        ],
        "connectives": ["首先", "其次", "然后", "最后", "第一", "第二", "因为", "所以", "总结", "举个例子"],
    },
    {
        "key": "execution",
        "name": "执行",
        "weight": 0.14,
        "definition": "是否讲清自己实际做了什么、结果是否可验证。",
        "evidence": ["文本：第一人称动作词", "文本：可量化结果", "文本：时间/规模等具体信息"],
        "scale": [
            (80, "有明确动作、有可验证结果"),
            (60, "有动作，结果偏模糊"),
            (40, "多是『我们』，个人贡献不清"),
            (0, "只有态度描述，无具体动作"),
        ],
        "action_words": ["我做了", "我写", "我改", "我设计", "我负责", "我实现", "我跑", "我查", "我压测", "我优化"],
    },
    {
        "key": "learning",
        "name": "学习",
        "weight": 0.12,
        "definition": "遇到不会的东西时，是否说得出学习路径与验证方式。",
        "evidence": ["文本：学习动作词", "文本：是否提到验证/对比", "agent：追问『怎么学的』后的回答"],
        "scale": [
            (80, "说得出路径、资料、以及怎么验证学会了"),
            (60, "说得出路径，缺验证"),
            (40, "只说『看了文档』，无细节"),
            (0, "没有可复述的学习过程"),
        ],
        "action_words": ["我查了", "我看了文档", "我照着", "我试了", "我对比", "我读了源码", "我复现"],
    },
    {
        "key": "patience",
        "name": "耐心",
        "weight": 0.10,
        "definition": "被反复追问、被指出错误时的稳定程度。",
        "evidence": ["音频：追问后的语速变化", "文本：追问后回答长度", "视频：追问段落的离席/转头"],
        "scale": [
            (80, "被追问后仍完整作答，长度不塌"),
            (60, "略有缩短但仍在回答"),
            (40, "明显变短、开始应付"),
            (0, "拒答或明显情绪化"),
        ],
        # 这条维度会影响面试节奏：耐心低时系统会延长面试（见 agent/nodes/pacing.py）
        "pacing_hook": "extend_when_low",
    },
    {
        "key": "stress",
        "name": "抗压",
        "weight": 0.10,
        "definition": "压力题（质疑、打断、限时）下是否还能保持输出质量。",
        "evidence": ["文本：压力题后的答案质量差值", "音频：压力段停顿占比", "视频：压力段视线稳定度"],
        "scale": [
            (80, "压力题后质量无明显下降"),
            (60, "略有下降但可接受"),
            (40, "明显下降，逻辑断裂"),
            (0, "无法继续作答"),
        ],
        "needs_pressure_round": True,
    },
    {
        "key": "adaptability",
        "name": "适应",
        "weight": 0.10,
        "definition": "题目条件变化后，是否调整思路而不是硬套旧答案。",
        "evidence": ["文本：变更题与原始题答案的差异度", "agent：条件变更后的追问表现"],
        "scale": [
            (80, "主动按新条件重构方案"),
            (60, "能调整，但需要提示"),
            (40, "部分沿用旧答案"),
            (0, "完全无视条件变化"),
        ],
        "needs_variant_round": True,
    },
]

DIMENSION_MAP: dict[str, dict] = {d["key"]: d for d in DIMENSIONS}

# 两个"风险信号"单独建模：它们不是能力，不需要打分，但影响面试官的判断。
RISK_SIGNALS: list[dict] = [
    {
        "key": "eye_contact",
        "name": "看镜头",
        "definition": "候选人视线是否在摄像头方向。",
        "evidence": ["视频：人脸中心偏移", "视频：人脸缺失累计时长"],
        "thresholds": {"good": 0.65, "warn": 0.35},  # 达标比例
    },
    {
        "key": "script_risk",
        "name": "读稿/外部提示风险",
        "definition": "是否存在疑似读稿迹象（视线长时间偏离、语速与停顿模式异常、回答与问题脱节）。",
        "evidence": ["视频：长时间侧视", "音频：长停顿后突然流利", "文本：书面语密度异常"],
        "thresholds": {"warn": 0.45, "high": 0.7},  # 风险分
    },
]

# 权重校验：必须为 1，否则报告里的加权总分不可解释
assert abs(sum(d["weight"] for d in DIMENSIONS) - 1.0) < 1e-9, "维度权重之和必须为 1"


# ---------------------------------------------------------------------------
# 面试节奏参数（人工可调）
# ---------------------------------------------------------------------------
@dataclass
class InterviewConfig:
    """一场面试的可调参数。前端"面试参数"面板直接改这些字段。"""

    job_role: str = "后端开发实习生"
    difficulty: str = "medium"              # easy | medium | hard
    base_minutes: int = 20                  # 基准时长
    max_minutes: int = 45                   # 上限（延长封顶）
    questions_target: int = 6               # 目标主问题数
    followup_max: int = 2                   # 单题最多追问轮次
    enabled_dimensions: list[str] = field(default_factory=lambda: [d["key"] for d in DIMENSIONS])
    weights: dict[str, float] = field(default_factory=lambda: {d["key"]: d["weight"] for d in DIMENSIONS})
    # 人工加权：某些岗位可临时抬高某维度，重算权重
    # 下面这三项是给面试官"过程中的干预"用的
    eye_contact_required: bool = True        # 要求看镜头，否则扣 risk
    script_risk_check: bool = True
    pressure_round: bool = True              # 是否插入压力题
    variant_round: bool = True               # 是否插入条件变更题
    voice_enabled: bool = True               # 是否启用语音链路

    def effective_weights(self) -> dict[str, float]:
        """只保留启用的维度并重新归一化。"""
        kept = {k: v for k, v in self.weights.items() if k in self.enabled_dimensions}
        total = sum(kept.values())
        if total <= 0:
            return {}
        return {k: v / total for k, v in kept.items()}

    def validate(self) -> None:
        if not 5 <= self.base_minutes <= 120:
            raise ValueError("base_minutes 需在 5..120")
        if self.max_minutes < self.base_minutes:
            raise ValueError("max_minutes 不能小于 base_minutes")
        if not 1 <= self.questions_target <= 20:
            raise ValueError("questions_target 需在 1..20")
        if not 0 <= self.followup_max <= 5:
            raise ValueError("followup_max 需在 0..5")
        unknown = set(self.enabled_dimensions) - set(DIMENSION_MAP)
        if unknown:
            raise ValueError(f"未知维度: {sorted(unknown)}")
        if not self.enabled_dimensions:
            raise ValueError("至少要启用一个维度")


# ---------------------------------------------------------------------------
# 模型定价（成本核算用；单位：元 / 千 token 或 元 / 分钟）
# 来源：通义千问公开价格页，按 2026-11 口径手工抄录，仅用于 demo 的内部估算。
# ---------------------------------------------------------------------------
PRICING = {
    "qwen-max": {"input_per_1k": 0.0024, "output_per_1k": 0.0096},
    "qwen-plus": {"input_per_1k": 0.0008, "output_per_1k": 0.002},
    "qwen3-vl-plus": {"input_per_1k": 0.003, "output_per_1k": 0.009},
    "qwen3.5-omni-flash-realtime": {"per_minute": 0.05},  # 按音频分钟计
    "mock": {"input_per_1k": 0.0, "output_per_1k": 0.0, "per_minute": 0.0},
}


def price_of(model: str) -> dict:
    """按模型名取价目；取不到就返回全 0（未知模型不该让成本核算报错）。

    故意不抛异常：模型名是部署时用环境变量换的，写错一个字不应该让整场
    面试在最后一步「算钱」时崩掉。
    """
    for key, val in PRICING.items():
        if model == key or model.startswith(key):
            return val
    return {"input_per_1k": 0.0, "output_per_1k": 0.0, "per_minute": 0.0}


@dataclass
class Settings:
    """运行时设置。全部可用环境变量覆盖，方便部署时不改代码。"""

    host: str = os.getenv("SI_HOST", "0.0.0.0")
    port: int = int(os.getenv("SI_PORT", "8090"))
    # provider: auto | mock | dashscope
    provider: str = os.getenv("SI_PROVIDER", "auto")
    dashscope_api_key: str = os.getenv("DASHSCOPE_API_KEY", "")
    text_model: str = os.getenv("SI_TEXT_MODEL", "qwen-plus")
    # 视觉模型：qwen3-vl-plus 是当前账号实测可用的；qwen-vl-max 在部分账号返回
    # 「Model not exist / Access denied」，所以默认值改成实测能跑的那个。
    vision_model: str = os.getenv("SI_VISION_MODEL", "qwen3-vl-plus")
    # 实时语音：qwen-omni-turbo-realtime 已下线（会话建立后立刻 InvalidParameter），
    # 改用 qwen3.5-omni-flash-realtime。注意必须同时指定 voice，否则
    # update_session 发出去的是 voice='null'，服务端报 "Voice 'null' is not supported"。
    omni_model: str = os.getenv("SI_OMNI_MODEL", "qwen3.5-omni-flash-realtime")
    omni_voice: str = os.getenv("SI_OMNI_VOICE", "Tina")
    db_path: Path = Path(os.getenv("SI_DB", str(DATA_DIR / "interview.db")))
    # 视频分析节流：每 N 毫秒最多分析一帧，避免把 CPU 打满
    vision_min_interval_ms: int = int(os.getenv("SI_VISION_MIN_INTERVAL_MS", "1200"))
    # 是否额外用视觉模型判帧（默认关）。
    # 关着的原因：本地启发式已经能回答"在不在画面、大概看哪"，而云端逐帧
    # 是 ¥1.36/场（占整场成本 57%）。开着的话按下面这个间隔抽样，
    # 用它补一个"头部朝向"这种本地算不出来的判断。
    vision_use_model: bool = os.getenv("SI_VISION_USE_MODEL", "0") == "1"
    vision_model_interval_ms: int = int(os.getenv("SI_VISION_MODEL_INTERVAL_MS", "8000"))
    # 一次面试最多缓存多少帧分析结果，防止内存涨
    vision_keep_frames: int = int(os.getenv("SI_VISION_KEEP_FRAMES", "1200"))
    # 是否把每帧原图落盘（默认关闭，demo 里只在需要复盘时开）
    keep_frames: bool = os.getenv("SI_KEEP_FRAMES", "0") == "1"
    # 单场面试的模型花费上限（元）。超过就把后续调用降级成规则化实现，
    # 但**不中断面试**——候选人答到一半被踢出去，比多花两毛钱严重得多。
    budget_cny: float = float(os.getenv("SI_BUDGET_CNY", "1.0"))
    # 每个租户每月可用场次。商用默认值，演示用 20 场足够。
    monthly_quota: int = int(os.getenv("SI_MONTHLY_QUOTA", "20"))

    def resolved_provider(self) -> str:
        if self.provider != "auto":
            return self.provider
        return "dashscope" if self.dashscope_api_key else "mock"


settings = Settings()
