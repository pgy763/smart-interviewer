"""简历接入：把 PDF / DOCX / 纯文本简历变成结构化档案。

需求原文是「能从提供的简历、报告等接入 demo，通过 AI 辅助分析」。
所以这一步不是"让模型读一遍简历写段简介"，而是**先抽取结构，再让结构进入
出题与评分链路**。理由是：

1. 出题要用到"简历里有哪些项目/技术栈"，那必须是字段，不能是一段散文；
2. 报告要回答"简历里提到的 3 个项目被问到了几个"，这需要可对齐的条目；
3. 抽取规则本身要能被单测覆盖，而模型输出不可重放。

分两层：
- `extract_text()`  纯 I/O：PDF/DOCX/TXT → 纯文本（不调模型，可单测）
- `build_profile()` 规则抽取：分段 → 技能/项目/经历（不调模型，可单测）
- `enhance_with_llm()` 可选：有 key 时让模型补"一句话画像 + 3 个建议追问点"，
  失败就退回规则结果，不阻塞面试创建。

这条"规则保底、模型增强"的分工贯穿整个项目，见 docs/DESIGN.md。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

# 技术支持：一份简历最多这么多字符进 prompt，避免把几千字的简历全塞进去
MAX_RESUME_CHARS = 1200
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8MB，和 im 项目里 MAX_BODY_LENGTH 同一个量级考虑

# 常见技术栈词典：命中即视为"简历里提到的技能"。
# 不追求全，追求"命中率高、误报低"——漏一些没关系，多抽错的会污染出题。
SKILL_DICT = [
    "Java", "Python", "Go", "C++", "C", "JavaScript", "TypeScript", "SQL",
    "Spring", "Spring Boot", "Spring Cloud", "MyBatis", "Netty", "Dubbo",
    "MySQL", "PostgreSQL", "Redis", "MongoDB", "Elasticsearch", "Kafka",
    "RabbitMQ", "RocketMQ", "Docker", "Kubernetes", "K8s", "Linux", "Nginx",
    "Vue", "React", "Node.js", "FastAPI", "Django", "Flask",
    "LangChain", "LangGraph", "RAG", "LLM", "PyTorch", "TensorFlow",
    "Git", "Jenkins", "Prometheus", "Grafana", "SkyWalking",
    "JVM", "GC", "多线程", "并发", "分布式", "微服务", "高可用",
]

# 简历里常见的段落标题。中文简历格式高度不统一，所以用"包含式"匹配而不是精确相等。
SECTION_HINTS = {
    "education": ["教育背景", "教育经历", "学历"],
    "skills": ["专业技能", "技能", "技术栈", "掌握技能"],
    "projects": ["项目经历", "项目经验", "主要项目", "项目"],
    "experience": ["实习经历", "工作经历", "工作背景"],
    "awards": ["荣誉", "获奖", "证书", "奖学金"],
}


@dataclass
class ResumeProfile:
    """结构化简历档案。字段全部可选——抽不到就是空，不编。"""

    name: str = ""
    years: float | None = None          # 从"2024.09-2028.06"这类区间推出来的年限
    education: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)   # [{title, bullets, tech}]
    awards: list[str] = field(default_factory=list)
    raw_chars: int = 0
    source: str = ""                    # pdf | docx | txt | pasted
    warnings: list[str] = field(default_factory=list)   # 解析降级说明，报告里要展示
    llm_summary: str = ""
    llm_probe_points: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "years": self.years,
            "education": self.education,
            "skills": self.skills,
            "projects": self.projects,
            "awards": self.awards,
            "raw_chars": self.raw_chars,
            "source": self.source,
            "warnings": self.warnings,
            "llm_summary": self.llm_summary,
            "llm_probe_points": self.llm_probe_points,
        }

    def prompt_text(self, limit: int = MAX_RESUME_CHARS) -> str:
        """给 prompt 用的紧凑摘要。

        注意这里**不是**把原文截断丢过去：原文里的空行、页眉页脚、无关的
        自我评价会稀释模型的注意力。摘要只保留技能 + 项目标题 + 每个项目
        前两条要点，这两样才是出题的依据。
        """
        parts: list[str] = []
        if self.skills:
            parts.append("技能：" + "、".join(self.skills[:18]))
        for p in self.projects[:4]:
            head = p.get("title") or "项目"
            bullets = [b for b in (p.get("bullets") or [])[:2] if b]
            line = head + ("；" + "；".join(bullets) if bullets else "")
            parts.append("项目：" + line)
        if self.years is not None:
            parts.append(f"经验：约 {self.years:g} 年")
        text = "\n".join(parts) if parts else (self.name or "")
        return text[:limit]


def extract_text(data: bytes, filename: str = "") -> tuple[str, str]:
    """从上传的字节流里抽纯文本，返回 (文本, 来源类型)。

    三种格式的路径：
    - PDF  用 pypdf 逐页 extract_text
    - DOCX 用 python-docx 读段落 + 表格单元格（很多人把技能塞进表格）
    - 其它 按 UTF-8 解码，失败再试 GBK（国内简历常见）

    抽不出来就抛 ValueError，由调用方决定是报错还是退回手填——不静默返回空串，
    因为"上传了但解析失败"和"没上传"对用户是两件事。
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"简历文件过大（>{MAX_UPLOAD_BYTES // 1024 // 1024}MB）")
    lower = (filename or "").lower()

    if lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError as e:  # pragma: no cover - 环境问题
            raise ValueError(f"解析 PDF 需要 pypdf：{e}") from e
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [(p.extract_text() or "") for p in reader.pages]
            text = "\n".join(pages).strip()
        except ValueError:
            raise
        except Exception as e:
            # pypdf 对"根本不是 PDF 的字节"会抛自己的异常类型（不止一种），
            # 这里统一成 ValueError：调用方只需要知道"这个文件读不了"。
            # 不统一的话，上层 `except ValueError` 兜不住，接口直接 500。
            raise ValueError(f"PDF 解析失败（文件可能已损坏或不是 PDF）：{e}") from e
        if not text:
            raise ValueError("PDF 里没有可抽取的文字（可能是扫描件/图片版简历）")
        return text, "pdf"

    if lower.endswith(".docx"):
        try:
            import docx  # python-docx
        except ImportError as e:  # pragma: no cover
            raise ValueError(f"解析 DOCX 需要 python-docx：{e}") from e
        try:
            d = docx.Document(io.BytesIO(data))
            chunks = [p.text for p in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    chunks.append(" | ".join(c.text.strip() for c in row.cells))
            text = "\n".join(c for c in chunks if c and c.strip()).strip()
        except ValueError:
            raise
        except Exception as e:
            # 同上：python-docx 对坏文件抛的类型很杂（BadZipFile / KeyError /
            # XMLSyntaxError），统一成 ValueError 让上层能兜住。
            raise ValueError(f"DOCX 解析失败（文件可能已损坏）：{e}") from e
        if not text:
            raise ValueError("DOCX 里没有可抽取的文字")
        return text, "docx"

    if lower.endswith(".doc"):
        # 老式 .doc 是二进制格式，python-docx 读不了。明确报错比瞎猜好。
        raise ValueError("暂不支持 .doc 老格式，请另存为 .docx 或 PDF")

    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc).strip(), "txt"
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别的文本编码（试过 utf-8 / gbk）")


def _split_sections(text: str) -> dict[str, list[str]]:
    """按段落标题把简历切成若干块。

    简历没有标准结构，这里的策略是：逐行扫描，遇到"包含某类标题关键字"的短行
    （<= 12 字）就切换当前块。切不出来也没关系——所有行都会落到 `_other`，
    后面的规则抽取仍然能在全文上跑。
    """
    lines = [ln.strip() for ln in text.splitlines()]
    blocks: dict[str, list[str]] = {"_other": []}
    cur = "_other"
    for ln in lines:
        if not ln:
            continue
        if len(ln) <= 12:
            for key, hints in SECTION_HINTS.items():
                if any(h in ln for h in hints):
                    cur = key
                    blocks.setdefault(cur, [])
                    break
            else:
                blocks.setdefault(cur, []).append(ln)
                continue
            continue
        blocks.setdefault(cur, []).append(ln)
    return blocks


def _extract_skills(text: str) -> list[str]:
    """词典命中 + 去重保序。

    大小写不敏感匹配，但输出用词典里的标准写法（'spring boot' → 'Spring Boot'），
    这样报告里的技能列表干净，不会同一项出现三种写法。
    """
    low = text.lower()
    seen: list[str] = []
    for skill in SKILL_DICT:
        if skill.lower() in low and skill not in seen:
            seen.append(skill)
    # 长的排前面（'Spring Boot' 在 'Spring' 之前），避免列表里出现父子项倒序
    return sorted(seen, key=lambda s: -len(s))


_PROJECT_LINE = re.compile(
    r"(?P<title>[^\n·•\-|]{2,40}?(?:系统|平台|服务|模块|引擎|助手|项目|Demo|demo|App|小程序))"
)


def _extract_projects(blocks: dict[str, list[str]], text: str) -> list[dict]:
    """抽项目条目。

    两条路径：
    1. 有明确的"项目经历"块 → 块内以 ·/•/- 开头的行当要点，其余短行当标题；
    2. 没有该块 → 用正则在全文中找"……系统/平台/服务"这类短语，兜底给 0~3 条。

    抽不准是可以接受的：项目条目的用途是"出题时点名问"，抽错了最多是问偏一点，
    而抽不到会导致所有题都变成通用题。所以宁可有兜底。
    """
    out: list[dict] = []
    block = blocks.get("projects") or []

    cur: dict | None = None
    for ln in block:
        is_bullet = ln[:1] in "·•-—*" or re.match(r"^\d+[.、)]", ln)
        if is_bullet:
            if cur is None:
                cur = {"title": "", "bullets": [], "tech": []}
                out.append(cur)
            body = ln.lstrip("·•-—* ").strip()
            body = re.sub(r"^\d+[.、)]\s*", "", body)
            if body:
                cur["bullets"].append(body)
            continue
        # 非要点行 → 视为新项目标题（如果当前已有要点，就另起一条）
        if cur is None or cur["bullets"]:
            cur = {"title": ln[:60], "bullets": [], "tech": []}
            out.append(cur)
        else:
            cur["title"] = (cur["title"] + " " + ln).strip()[:60]

    if not out:
        for m in _PROJECT_LINE.finditer(text):
            title = m.group("title").strip()
            if not title or len(title) < 4:
                continue
            if any(title == p["title"] for p in out):
                continue
            out.append({"title": title[:60], "bullets": [], "tech": []})
            if len(out) >= 3:
                break

    # 每条项目回填它自己提到的技术栈（用于"这题问的是项目 A 的 Redis 还是项目 B 的"）
    for p in out:
        blob = p["title"] + "\n" + "\n".join(p["bullets"])
        p["tech"] = [s for s in SKILL_DICT if s.lower() in blob.lower()][:6]
        p["bullets"] = [b[:160] for b in p["bullets"][:6]]
    return out[:6]


_YEAR_RANGE = re.compile(r"(20\d{2})[.\-/年](\d{1,2})?.*?(20\d{2})[.\-/年](\d{1,2})?")


def _extract_years(blocks: dict[str, list[str]]) -> float | None:
    """从「实习/工作经历」块里推年限。

    只在 experience 块里找，**不在全文里找**：学生简历里最显眼的年份区间
    是教育经历（2024.09-2028.06），按全文找会算出"3.8 年经验"这种东西，
    那比不写更糟——它会让面试官以为简历在注水。
    推不出来就返回 None，报告里写"未识别"。
    """
    block = blocks.get("experience") or []
    if not block:
        return None
    best = 0.0
    for ln in block:
        for m in _YEAR_RANGE.finditer(ln):
            y1, m1, y2, m2 = m.group(1), m.group(2), m.group(3), m.group(4)
            try:
                a = int(y1) + (int(m1) - 1) / 12 if m1 else int(y1)
                b = int(y2) + (int(m2) - 1) / 12 if m2 else int(y2)
            except (TypeError, ValueError):
                continue
            span = b - a
            if 0 < span <= 8:
                best = max(best, span)
    return round(best, 1) if best else None


def build_profile(text: str, source: str = "txt", filename: str = "") -> ResumeProfile:
    """规则抽取主入口。纯函数：同样的文本永远同样的结果，可单测。"""
    text = (text or "").strip()
    prof = ResumeProfile(raw_chars=len(text), source=source)
    if not text:
        prof.warnings.append("简历内容为空")
        return prof

    blocks = _split_sections(text)
    prof.skills = _extract_skills(text)
    prof.projects = _extract_projects(blocks, text)
    prof.years = _extract_years(blocks)

    edu = blocks.get("education") or []
    prof.education = [ln[:80] for ln in edu[:4]]
    awards = blocks.get("awards") or []
    prof.awards = [ln[:80] for ln in awards[:4]]

    # 姓名：取第一个"2~4 个汉字且不含数字/标点"的短行，且不是段落标题
    heads = {h for hints in SECTION_HINTS.values() for h in hints}
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s in heads or len(s) > 6:
            continue
        if re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", s):
            prof.name = s
            break

    if not prof.skills:
        prof.warnings.append("没有识别到技术栈关键词，出题会退化成岗位通用题")
    if not prof.projects:
        prof.warnings.append("没有识别到项目条目，追问无法点名到具体项目")
    if prof.years is None:
        prof.warnings.append("没有识别到经历年限")
    return prof


def enhance_with_llm(profile: ResumeProfile, provider) -> ResumeProfile:
    """可选增强：让模型补"一句话画像 + 3 个值得追问的点"。

    失败**不抛异常**——简历解析失败不该让面试创建不了。
    增强结果只进 prompt 与报告，不参与打分，所以模型编了也不会污染分数。
    """
    if not profile.skills and not profile.projects:
        # 规则层什么都没抽到，交给模型也大概率是幻觉，直接跳过
        return profile
    try:
        ctx = {
            "resume": profile.prompt_text(),
            "job_role": "",
        }
        r = provider.resume_digest(ctx) if hasattr(provider, "resume_digest") else None
        if r is not None and r.ok and isinstance(r.data, dict):
            profile.llm_summary = str(r.data.get("summary") or "")[:300]
            pts = r.data.get("probe_points") or []
            profile.llm_probe_points = [str(p)[:120] for p in pts][:3]
    except Exception as e:  # pragma: no cover - 防御性
        profile.warnings.append(f"模型增强失败，已使用规则结果：{type(e).__name__}")
    return profile


def profile_from_any(
    *,
    text: str = "",
    data: bytes | None = None,
    filename: str = "",
    provider: Any | None = None,
) -> ResumeProfile:
    """对外统一入口：要么给文本，要么给文件字节。"""
    warnings: list[str] = []
    if data:
        try:
            extracted, source = extract_text(data, filename)
        except ValueError as e:
            # 文件解析失败 → 退回手填文本（如果有），并把原因记进 warnings
            warnings.append(str(e))
            extracted, source = (text or ""), "pasted"
    else:
        extracted, source = (text or ""), "pasted"

    prof = build_profile(extracted, source=source, filename=filename)
    prof.warnings = warnings + prof.warnings
    if provider is not None:
        prof = enhance_with_llm(prof, provider)
    return prof
