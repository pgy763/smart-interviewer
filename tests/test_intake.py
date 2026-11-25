"""接入层与导出的单测。

这一层最容易出错的不是"正常简历"，而是各种边角：扫描件 PDF（抽不出字）、
老式 .doc、超长文本、报告只导出了一半、上一轮报告里 dimensions 是空数组。
这些情况在真实面试里都会出现，而且一旦抛异常，整场面试就开不了。

所以这里的用例大多是"坏输入应该优雅退化"，而不是"好输入应该输出什么"。
"""

from __future__ import annotations

import io
import json

import unittest
from pathlib import Path

from app.intake import build_prior_report, parse_report_json, profile_from_any
from app.intake.resume import MAX_UPLOAD_BYTES, ResumeProfile, build_profile, extract_text
from app.report import export as ex


def _make_docx(lines: list[str]) -> bytes:
    import docx

    d = docx.Document()
    for ln in lines:
        d.add_paragraph(ln)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _make_pdf(lines: list[str]) -> bytes:
    """手搓一个最小 PDF。不引 reportlab（装不上），也不测复杂排版。"""
    text_ops = " ".join(f"({ln}) Tj 0 -18 Td" for ln in lines)
    content = f"BT /F1 12 Tf 40 780 Td {text_ops} ET".encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n"
            f"{xref_at}\n%%EOF\n").encode()
    return bytes(out)


RESUME_TEXT = """文博玉
中北大学 软件工程 2024.09-2028.06
专业技能
Java Python Spring Boot Netty MySQL Redis Docker Kafka
工作经历
某科技公司 后端开发实习生 2025.07-2025.11
项目经历
鱼见 IM 即时通讯系统
· 基于 Netty 自研 18 字节二进制协议
· Redis INCR 发号 + 三层幂等去重
秒杀交易平台
· Redis 预扣库存，MQ 异步下单
"""


class TestExtractText(unittest.TestCase):
    def test_docx(self):
        data = _make_docx(["张三", "专业技能", "Java Redis"])
        text, src = extract_text(data, "a.docx")
        self.assertEqual(src, "docx")
        self.assertIn("Java", text)
        self.assertIn("张三", text)

    def test_pdf(self):
        data = _make_pdf(["Zhang San", "Java Redis Netty"])
        text, src = extract_text(data, "a.pdf")
        self.assertEqual(src, "pdf")
        self.assertIn("Java", text)

    def test_doc_old_format_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            extract_text(b"\xd0\xcf\x11\xe0", "old.doc")
        self.assertIn(".doc", str(ctx.exception))

    def test_too_large(self):
        big = b"x" * (MAX_UPLOAD_BYTES + 10)
        with self.assertRaises(ValueError):
            extract_text(big, "big.txt")

    def test_plain_text_gbk(self):
        text, src = extract_text("文博玉 会 Java".encode("gbk"), "r.txt")
        self.assertEqual(src, "txt")
        self.assertIn("文博玉", text)

    def test_undecodable(self):
        with self.assertRaises(ValueError):
            extract_text(b"\xff\xfe\x00\x00\xff", "r.txt")


class TestBuildProfile(unittest.TestCase):
    def test_skills_and_projects(self):
        p = build_profile(RESUME_TEXT, source="pasted")
        self.assertIn("Netty", p.skills)
        self.assertIn("Spring Boot", p.skills)
        self.assertEqual([x["title"] for x in p.projects][:2],
                         ["鱼见 IM 即时通讯系统", "秒杀交易平台"])
        self.assertEqual(p.name, "文博玉")

    def test_years_only_from_experience_block(self):
        # 教育经历 2024.09-2028.06 不应被算成工作经验
        p = build_profile(RESUME_TEXT, source="pasted")
        self.assertIsNotNone(p.years)
        self.assertLess(p.years, 1.0)
        # 2025.07-2025.11 = 4 个月 ≈ 0.33，四舍五入到 0.3
        self.assertAlmostEqual(p.years, 0.3, places=1)

    def test_no_experience_gives_none_and_warning(self):
        p = build_profile("张三\n专业技能\nJava", source="pasted")
        self.assertIsNone(p.years)
        self.assertIn("没有识别到经历年限", p.warnings)

    def test_prompt_text_does_not_leak_full_resume(self):
        p = build_profile(RESUME_TEXT, source="pasted")
        s = p.prompt_text()
        self.assertIn("Netty", s)
        self.assertLess(len(s), len(RESUME_TEXT))

    def test_empty_text(self):
        p = build_profile("", source="pasted")
        self.assertEqual(p.skills, [])
        self.assertIsInstance(p.warnings, list)


class TestProfileFromAny(unittest.TestCase):
    def test_file_wins_over_text(self):
        data = _make_docx(["李四", "专业技能", "Go Kubernetes"])
        p = profile_from_any(text="王五 Java", data=data, filename="x.docx")
        self.assertIn("Kubernetes", p.skills)

    def test_bad_file_falls_back_to_text(self):
        p = profile_from_any(text="赵六 Netty", data=b"not a pdf",
                             filename="broken.pdf")
        self.assertIn("Netty", p.skills)
        self.assertTrue(p.warnings)  # 解析失败原因必须留在 warnings 里

    def test_no_input(self):
        p = profile_from_any()
        self.assertIsInstance(p, ResumeProfile)
        self.assertEqual(p.skills, [])


class TestPriorReport(unittest.TestCase):
    def _report(self) -> dict:
        return {
            "interview": {"id": "abc", "candidate": "张三", "job_role": "后端"},
            "verdict": {"kind": "ok", "text": "正常完成"},
            "overall": 71.5,
            "dimensions": [
                {"key": "learning", "name": "学习", "status": "insufficient_evidence",
                 "score": None, "weight": 0.12},
                {"key": "comprehension", "name": "理解", "status": "scored",
                 "score": 82.0, "weight": 0.16},
            ],
            "insufficient_names": ["学习"],
            "risk": {"level": "low", "reasons": [], "score": 0.1},
            "fallbacks": {},
            "timeline": [
                {"idx": 0, "phase": "main", "phase_label": "主问题",
                 "question": "讲讲 Netty", "answer": "好", "answered": True,
                 "key_points": [], "has_number": False, "has_example": False,
                 "off_topic": False},
            ],
        }

    def test_parse_variants(self):
        rep = self._report()
        for raw in (json.dumps(rep, ensure_ascii=False),
                    json.dumps({"ok": True, "report": rep}, ensure_ascii=False),
                    rep):
            parsed = parse_report_json(raw)
            self.assertEqual(parsed["overall"], 71.5)

    def test_build_prior(self):
        pr = build_prior_report(self._report())
        self.assertEqual(pr.overall, 71.5)
        # scored_dims 是 {维度名: 分数} 的映射，不是列表
        self.assertEqual(pr.scored_dims, {"理解": 82.0})
        self.assertEqual(pr.insufficient_dims, ["学习"])
        self.assertTrue(pr.probe_points)

    def test_partial_report_does_not_raise(self):
        pr = build_prior_report({"overall": 60})
        self.assertEqual(pr.overall, 60)
        self.assertEqual(pr.scored_dims, {})
        self.assertEqual(pr.insufficient_dims, [])

    def test_empty_report(self):
        pr = build_prior_report({})
        self.assertIsNone(pr.overall)
        self.assertEqual(pr.probe_points, [])

    def test_bad_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_report_json("这不是 JSON")


class TestExport(unittest.TestCase):
    def _report(self) -> dict:
        return {
            "interview": {"id": "i1", "candidate": "张三", "job_role": "后端",
                          "duration_sec": 900, "provider": "mock"},
            "verdict": {"kind": "ok", "text": "正常完成。"},
            "overall": 66.5,
            "overall_label": "中等",
            "dimensions": [
                {"key": "comprehension", "name": "理解", "score": 70.0,
                 "status": "scored", "weight": 0.16, "reason": "准确",
                 "evidence": [{"kind": "text", "text": "回答切题", "value": 1,
                               "turn_idx": 0}]},
                {"key": "learning", "name": "学习", "score": None,
                 "status": "insufficient_evidence", "weight": 0.12,
                 "reason": "没有学习动作", "evidence": []},
            ],
            "scored_count": 1,
            "insufficient_names": ["学习"],
            "weights_effective": {"comprehension": 1.0},
            "fallbacks": {"no_video": "本场没有可用画面"},
            "fallback_summary": [{"code": "no_video", "title": "无画面",
                                  "desc": "没有收到视频帧", "detail": "0 帧"}],
            "risk": {"score": 0.1, "level": "low", "reasons": [],
                     "vision": {"available": False},
                     "audio_headline": {"speed_chars_per_sec": 4.2,
                                        "pause_ratio": 0.1}},
            "timeline": [{"idx": 0, "phase": "main", "phase_label": "主问题",
                          "question": "讲讲你的项目", "answer": "我做了 IM",
                          "answered": True, "key_points": ["IM"], "has_number": False,
                          "has_example": False, "off_topic": False, "latency_ms": 100}],
            "pacing": "实际时长 15.0 分钟，在基准 20.0 分钟附近。",
            "cost": {"total_cny": 0.5, "by_purpose": {"question": 0.1},
                     "by_model": {"qwen-plus": 0.5}, "calls": 12,
                     "unpriced_models": ["qwen9-foo"]},
            "budget": {"limit_cny": 1.0, "spent_cny": 0.5, "tripped": 2,
                       "exhausted": False},
            "material": {"resume": {"source": "docx", "chars": 900,
                                    "skills": ["Java", "Netty"], "projects": ["鱼见 IM"],
                                    "years": 0.4, "llm_summary": "后端方向",
                                    "warnings": []},
                         "prior_report": {"source_id": "prev", "overall": 60.0,
                                          "insufficient_dims": ["学习"],
                                          "probe_points": ["再问 Netty"]}},
            "provider": "mock",
            "limitations": ["不做录用判断。"],
            "generated_at": 1_760_000_000,
        }

    def test_json_roundtrip(self):
        rep = self._report()
        body, ctype, ext = ex.export(rep, "json")
        self.assertIn("application/json", ctype)
        self.assertEqual(ext, "json")
        parsed = json.loads(body)
        self.assertEqual(parsed["overall"], 66.5)

    def test_markdown(self):
        body, ctype, ext = ex.export(self._report(), "md")
        self.assertEqual(ext, "md")
        self.assertIn("# AI 面试分析报告：张三", body)
        self.assertIn("66.5", body)
        self.assertIn("学习", body)
        # 不评分的维度不能显示成 0 分
        self.assertIn("不评分", body)
        self.assertIn("预算上限", body)
        self.assertIn("本场有 2 次调用因超出预算被降级", body)
        self.assertIn("本次面试的输入材料", body)

    def test_html_is_standalone(self):
        body, ctype, ext = ex.export(self._report(), "html")
        self.assertEqual(ext, "html")
        self.assertIn("<!doctype html>", body.lower())
        self.assertIn("<style>", body)
        self.assertNotIn("<script", body.lower())
        self.assertIn("预算上限", body)

    def test_unknown_format_falls_back_to_json(self):
        body, ctype, ext = ex.export(self._report(), "xlsx")
        self.assertEqual(ext, "json")
        self.assertIn("overall", json.loads(body))

    def test_filename_sanitizes(self):
        rep = self._report()
        rep["interview"]["candidate"] = "张/三:?"
        name = ex.filename(rep, "md")
        self.assertTrue(name.endswith(".md"))
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, name)

    def test_handles_missing_sections(self):
        # 一份只有总数的极简报告也不能让导出崩
        for fmt in ("json", "md", "html"):
            body, _, _ = ex.export({"overall": 50}, fmt)
            self.assertTrue(body)

    def test_dimension_reference(self):
        rows = ex.dimension_reference()
        self.assertEqual(len(rows), 8)
        self.assertIn("key", rows[0])


class TestSchemaMigration(unittest.TestCase):
    """加列迁移：老库（没有 resume_profile_json 等列）升级后要能直接用。"""

    def test_migrate_adds_columns(self):
        import shutil
        import sqlite3

        from app import db

        # 不用 tempfile：这台机器上系统临时目录被沙箱拒绝（PermissionError）。
        # 改成工程内的 tests/_tmp，用完删掉。
        td = Path(__file__).resolve().parent / "_tmp" / "migrate"
        shutil.rmtree(td, ignore_errors=True)
        td.mkdir(parents=True, exist_ok=True)
        try:
            path = td / "old.db"
            # 造一个"老版本"的库：只有 interview 的原始列
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE interview (id TEXT PRIMARY KEY, candidate_name TEXT,"
                " job_role TEXT, resume_text TEXT, config_json TEXT, status TEXT,"
                " provider TEXT, started_at REAL, ended_at REAL, abort_reason TEXT,"
                " fallback_json TEXT)"
            )
            conn.execute("INSERT INTO interview (id, candidate_name) VALUES ('x','老数据')")
            conn.commit()
            conn.close()

            db.init_db(path)
            check = sqlite3.connect(path)
            check.row_factory = sqlite3.Row  # db._columns 用 row["name"] 取值
            try:
                cols = db._columns(check, "interview")
            finally:
                check.close()
            self.assertIn("resume_profile_json", cols)
            self.assertIn("prior_report_json", cols)
            self.assertIn("material_json", cols)
            # 老数据还在。get_interview 走的是默认连接（settings.db_path），
            # 所以这里要临时把默认库指到测试库，否则查的是 data/interview.db。
            old_path = db.settings.db_path
            try:
                db.settings.db_path = path
                self.assertEqual(db.get_interview("x")["candidate_name"], "老数据")
                # 幂等：再跑一次不报错
                db.init_db(path)
            finally:
                db.settings.db_path = old_path
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
