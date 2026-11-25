"""接入层：把外部物料（简历、上一轮报告）变成系统内部的结构化输入。

分成两个模块，各自只做一件事：

- `resume.py`  文件 → 结构化简历档案（技能/项目/经历），规则抽取 + 可选模型增强
- `report.py`  上一轮报告 JSON → `PriorReport`（缺口维度/风险/建议追问点）

为什么单独成包而不是塞进 agent/：
这是**系统的入口边界**，它决定了外面能喂进来什么、喂进来会被怎么解释。
把它和"面试怎么问"分开，才能单独测试"一份格式很怪的简历会不会把出题带偏"
这种问题，而不用起一个完整面试。
"""

from app.intake.resume import (  # noqa: F401
    MAX_RESUME_CHARS,
    MAX_UPLOAD_BYTES,
    ResumeProfile,
    build_profile,
    enhance_with_llm,
    extract_text,
    profile_from_any,
)
from app.intake.report import (  # noqa: F401
    PriorReport,
    build_prior_report,
    parse_report_json,
)

__all__ = [
    "MAX_RESUME_CHARS",
    "MAX_UPLOAD_BYTES",
    "ResumeProfile",
    "build_profile",
    "enhance_with_llm",
    "extract_text",
    "profile_from_any",
    "PriorReport",
    "build_prior_report",
    "parse_report_json",
]
