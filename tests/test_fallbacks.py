"""四种异常兜底 + 评分规则的单元测试。

用 unittest（环境里没有 pytest）。跑法：

    /d/Soft/conda/python.exe -m unittest discover -s tests -v

设计原则：每个用例都用**独立的临时数据库**，不碰 data/interview.db，
这样反复跑也不会互相污染（评分断言里有精确数字）。
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 不用 tempfile：沙箱下 Windows 临时目录的 rmtree 会因权限被拒
# （PermissionError: [WinError 5]），报告一片红但其实测试全过。
# 改用工作区内的固定目录，跑完只删里面的 db 文件。
TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


def fresh_env(tmpdir: str):
    """把 settings.db_path 指到临时文件，并重载依赖模块。

    模块里 db_path 是启动时读进 dataclass 的，光改环境变量没用，
    所以这里显式改 settings 再重载 app.db / app.api.session。
    """
    from app import config as cfg_mod

    cfg_mod.settings.db_path = Path(tmpdir) / "t.db"
    from app import db as db_mod
    from app.api import session as sess_mod

    importlib.reload(db_mod)
    importlib.reload(sess_mod)
    db_mod.init_db()
    return db_mod, sess_mod


class BaseCase(unittest.TestCase):
    def setUp(self):
        # 每个用例一个子目录，互不干扰
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._dir = TMP_ROOT / f"{self.__class__.__name__}_{id(self)}"
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.db, self.sess = fresh_env(str(self._dir))
        self.sess.manager = self.sess.SessionManager()
        self.sess.manager._sessions.clear()

    def tearDown(self):
        # 只删 db 文件，不 rmtree（沙箱下 rmtree 可能权限被拒）
        for p in self._dir.glob("t.db*"):
            try:
                p.unlink()
            except OSError:
                pass

    def make(self, **cfg):
        c = {"job_role": "后端开发实习生", "questions_target": 3, "followup_max": 1}
        c.update(cfg)
        return self.sess.manager.create("测试候选人", "后端开发实习生", "", c)


class TestFallbackNoProgress(BaseCase):
    """场景一：面试时长 0 —— 创建后立刻结束，一题没答。"""

    def test_instant_finish_gives_no_progress(self):
        out = self.make()
        iid = out["interview_id"]
        self.db.set_interview_status(iid, "finished")
        # 把 ended_at 写成和 started_at 相同，模拟"0 秒结束"
        with self.db.get_conn() as conn:
            row = conn.execute("SELECT started_at FROM interview WHERE id=?", (iid,)).fetchone()
            conn.execute("UPDATE interview SET ended_at=? WHERE id=?", (row["started_at"], iid))

        fb = self.sess.detect_fallbacks(iid)
        self.assertIn("no_progress", fb["flags"])
        self.assertTrue(fb["blocking"])
        self.assertEqual(fb["answered"], 0)

        rep = self.report(iid)
        self.assertEqual(rep["verdict"]["kind"], "insufficient_data")
        self.assertIsNone(rep["overall"])
        self.assertIn("没有产生有效问答", json.dumps(rep["fallback_summary"], ensure_ascii=False))

    def report(self, iid):
        from app.report.builder import build_report

        return build_report(iid)

    def test_fresh_interview_is_not_falsely_no_progress(self):
        """刚创建 0.2 秒的面试不能被误报成 no_progress（第一版就是这个 bug）。"""
        out = self.make()
        iid = out["interview_id"]
        time.sleep(0.2)
        fb = self.sess.detect_fallbacks(iid)
        # 还没答过题 → no_progress 会置位；但它是"没答题"，不是"0 秒结束"
        # 关键断言：不能因为 duration<1 就把它判成 ended_instant
        self.assertFalse(bool(self.db.get_interview(iid).get("ended_at")))
        self.assertIn("no_progress", fb["flags"])  # 因为确实没答题
        self.assertEqual(fb["flags"]["no_progress"]["answered"], 0)


class TestFallbackEmptyAudio(BaseCase):
    """场景二：录音为空 —— 答了题但没有文字（麦克风没授权）。"""

    def test_empty_answers(self):
        out = self.make(questions_target=2)
        iid = out["interview_id"]
        # 直接提交空字符串：后端会走"空回答 → 追问确认是否网络问题"的分支
        r = self.sess.manager.get(iid).submit("", 0)
        self.assertEqual(r["action"], "followup")

        # 用空白串反复提交，所有回答文本都为空
        self.sess.manager.get(iid).submit("   ", 0)

        turns = self.db.list_turns(iid)
        total_chars = sum(len((t.get("answer") or "").strip()) for t in turns)
        fb = self.sess.detect_fallbacks(iid)
        # 没有有效文本 → empty_audio
        self.assertEqual(total_chars, 0)
        self.assertIn("empty_audio", fb["flags"])
        self.assertTrue(fb["blocking"])


class TestFallbackCandidateLeft(BaseCase):
    """场景三：中途离开画面 —— 连续 20 秒没人脸。"""

    def test_away_detection(self):
        out = self.make()
        iid = out["interview_id"]
        now = time.time()
        # 造 25 秒的无人脸帧序列
        for i in range(6):
            self.db.add_vision_frame(iid, ts=now - 25 + i * 5, face_found=0,
                                     face_ratio=0, center_dx=0, center_dy=0,
                                     brightness=0.3, looking=0, motion=0.01,
                                     note="未检测到肤色连通块")
        self.db.add_vision_frame(iid, ts=now, face_found=1, face_ratio=0.12,
                                 center_dx=0.02, center_dy=0.0, brightness=0.4,
                                 looking=1, motion=0.02, note="居中")
        fb = self.sess.detect_fallbacks(iid)
        self.assertIn("candidate_left", fb["flags"])
        self.assertGreaterEqual(fb["flags"]["candidate_left"]["longest_away_sec"], 20)

    def test_short_away_is_not_flagged(self):
        """只离开 5 秒不该触发 —— 阈值是 20 秒。"""
        out = self.make()
        iid = out["interview_id"]
        now = time.time()
        for i in range(2):
            self.db.add_vision_frame(iid, ts=now - 5 + i * 2, face_found=0,
                                     face_ratio=0, center_dx=0, center_dy=0,
                                     brightness=0.3, looking=0, motion=0.01, note="")
        self.db.add_vision_frame(iid, ts=now, face_found=1, face_ratio=0.12,
                                 center_dx=0, center_dy=0, brightness=0.4,
                                 looking=1, motion=0.02, note="")
        fb = self.sess.detect_fallbacks(iid)
        self.assertNotIn("candidate_left", fb["flags"])


class TestFallbackNoVideo(BaseCase):
    """场景四：没有视频——摄像头未授权，报告必须说明。"""

    def test_no_video(self):
        out = self.make()
        iid = out["interview_id"]
        fb = self.sess.detect_fallbacks(iid)
        self.assertIn("no_video", fb["flags"])

        rep = self.report(iid)
        codes = [x["code"] for x in rep["fallback_summary"]]
        self.assertIn("no_video", codes)

    def report(self, iid):
        from app.report.builder import build_report

        return build_report(iid)


class TestFallbackDisconnected(BaseCase):
    """场景五：网络中断 —— 超过 3 分钟没有新事件。"""

    def test_stall_detected(self):
        out = self.make()
        iid = out["interview_id"]
        # 把 started_at 往前挪 10 分钟，且状态不是 finished
        with self.db.get_conn() as conn:
            conn.execute("UPDATE interview SET started_at=? WHERE id=?",
                         (time.time() - 600, iid))
        fb = self.sess.detect_fallbacks(iid)
        self.assertIn("disconnected", fb["flags"])
        self.assertGreaterEqual(fb["flags"]["disconnected"]["stalled_sec"], 180)

    def test_finished_interview_is_not_disconnected(self):
        out = self.make()
        iid = out["interview_id"]
        with self.db.get_conn() as conn:
            conn.execute("UPDATE interview SET started_at=? WHERE id=?",
                         (time.time() - 600, iid))
        self.db.set_interview_status(iid, "finished")
        fb = self.sess.detect_fallbacks(iid)
        self.assertNotIn("disconnected", fb["flags"])


class TestFallbackAborted(BaseCase):
    """主动中止：状态 aborted + 原因，报告要写出来。"""

    def test_aborted(self):
        out = self.make()
        iid = out["interview_id"]
        with self.db.get_conn() as conn:
            conn.execute("UPDATE interview SET status='aborted', abort_reason=? WHERE id=?",
                         ("候选人主动退出", iid))
        fb = self.sess.detect_fallbacks(iid)
        self.assertIn("aborted", fb["flags"])
        self.assertEqual(fb["flags"]["aborted"]["reason"], "候选人主动退出")


class TestScoringRules(BaseCase):
    """评分规则：证据不足不出分、加权只算已出分维度。"""

    def test_insufficient_without_rounds(self):
        """没有压力题 / 变更题 / 追问时，抗压/适应/耐心 必须是证据不足。"""
        out = self.make(questions_target=2)
        iid = out["interview_id"]
        s = self.sess.manager.get(iid)
        s.submit("我用 Redis 做发号，实测 P99 是 30 毫秒，跑了 50 条连接。", 8000)
        s.submit("粘包靠长度字段解决，半包靠 resetReaderIndex 退回去等数据。", 9000)

        from app.report.builder import build_report

        rep = build_report(iid)
        dims = {d["key"]: d for d in rep["dimensions"]}
        # 这三者需要专门环节，这一场没走到
        for k in ("patience", "stress", "adaptability"):
            self.assertEqual(dims[k]["status"], "insufficient_evidence",
                             f"{k} 不该出分：{dims[k]}")
            self.assertIsNone(dims[k]["score"])

        # 证据不足的维度不能进分母
        self.assertEqual(rep["scored_count"],
                         sum(1 for d in rep["dimensions"] if d["status"] == "scored"))

    def test_weight_normalization(self):
        """只启用两个维度时，权重必须归一化到 1。"""
        from app.config import InterviewConfig

        cfg = InterviewConfig(enabled_dimensions=["comprehension", "communication"])
        w = cfg.effective_weights()
        self.assertEqual(set(w), {"comprehension", "communication"})
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)
        # 原权重 0.16 / 0.14 → 归一化后 0.5333 / 0.4667
        self.assertAlmostEqual(w["comprehension"], 0.16 / 0.30, places=4)

    def test_all_insufficient_gives_none_overall(self):
        out = self.make(questions_target=1)
        iid = out["interview_id"]
        from app.report.builder import build_report

        rep = build_report(iid)
        self.assertIsNone(rep["overall"])
        self.assertEqual(rep["verdict"]["kind"], "insufficient_data")


class TestVisionHeuristics(unittest.TestCase):
    """视觉启发式：合成图能稳定判断"有没有脸"。"""

    def _img(self, cx_ratio=0.5, face=True):
        import base64
        import io

        from PIL import Image, ImageDraw

        img = Image.new("RGB", (640, 480), (46, 52, 64))
        if face:
            d = ImageDraw.Draw(img)
            cx = int(640 * cx_ratio)
            d.ellipse([cx - 95, 90, cx + 95, 340], fill=(222, 178, 148))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    def test_centered_face_looks_at_camera(self):
        from app.vision.analyzer import analyze_frame

        r, _ = analyze_frame(self._img(0.5), None)
        self.assertTrue(r.face_found)
        self.assertTrue(r.looking)
        self.assertLess(abs(r.center_dx), 0.05)

    def test_offcenter_face_does_not_look(self):
        from app.vision.analyzer import analyze_frame

        r, _ = analyze_frame(self._img(0.12), None)
        self.assertTrue(r.face_found)
        self.assertFalse(r.looking)

    def test_blank_frame_has_no_face(self):
        from app.vision.analyzer import analyze_frame

        r, _ = analyze_frame(self._img(face=False), None)
        self.assertFalse(r.face_found)

    def test_to_dict_is_json_serializable(self):
        """numpy 标量必须被转成原生类型，否则 JSONResponse 会 500。"""
        from app.vision.analyzer import analyze_frame

        r, _ = analyze_frame(self._img(0.5), None)
        d = r.to_dict()
        json.dumps(d)  # 不抛就是通过
        self.assertIs(type(d["face_found"]), bool)
        self.assertIs(type(d["looking"]), bool)
        self.assertIs(type(d["center_dx"]), float)

    def test_script_risk_reasons(self):
        from app.vision.analyzer import script_risk_score

        summary = {"frames": 30, "available": True, "looking_ratio": 0.2,
                   "away_ratio": 0.4, "longest_away_sec": 12,
                   "median_frame_gap_sec": 3, "motion_avg": 0.02,
                   "motion_p90": 0.05, "brightness_avg": 0.4, "dark_ratio": 0.1}
        audio = {"pause_ratio": 0.5, "speed_cv": 0.1}
        score, reasons = script_risk_score(summary, audio)
        self.assertGreater(score, 0.3)
        self.assertTrue(any("视线" in r for r in reasons))
        self.assertTrue(any("离开" in r for r in reasons))


class TestAudioRules(unittest.TestCase):
    def test_speed_stability(self):
        from app.audio.analyzer import speed_stability_score

        # 参数是 pause_features() 产出的汇总 dict，不是 speed 事件列表——
        # 稳定性要用变异系数，单个 speed 事件算不出来。
        self.assertIsNone(speed_stability_score({"speed_cv": None})[0])
        self.assertIsNone(speed_stability_score({})[0])
        s, _ = speed_stability_score({"speed_cv": 0.05})
        self.assertGreaterEqual(s, 70)
        slow, _ = speed_stability_score({"speed_cv": 0.8})
        self.assertLess(slow, 70)

    def test_filler_and_formal(self):
        from app.audio.analyzer import text_features

        f = text_features("嗯，那个，我觉得综上所述，因此这个方案显著更好。")
        self.assertGreater(f["fillers"], 0)
        self.assertGreater(f["formal_density"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
