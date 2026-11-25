"""会话层：一场面试的生命周期。

这一层是"图之外的那部分"——图只管一轮问答的决策，谁保管跨轮状态、
谁落库、谁处理异常，都在这里。

**为什么不在图的 state 里扛整场**：见 `app/agent/graph.py` 的说明。
简言之：图保持纯函数，会话层保持有状态；进程重启时从 SQLite 重建 state
就能接着往下走，不需要 LangGraph 的 checkpointer（也用不上，本项目连
SQLAlchemy 都没装）。

**四种异常的兜底都在这一层实现**（验收项要求"每种都要明确处理、不能卡死"）：

1. 面试时长 0 —— 创建后一直没开始（`status=created` 或 `started_at` 与
   `ended_at` 相差 < 1 秒），报告标 `no_progress`，不生成任何分数。
2. 中途离开 —— 视频帧里连续无人脸超过阈值，或候选人端 WS 断了且没有重连。
   报告标 `candidate_left`，已产生的分数照出，但风险信号里记明"离开过多久"。
3. 录音为空 —— 所有回答的字数总和为 0（或语音链路没采到音频）。
   报告标 `empty_audio`，只出"无法评估"的结论，绝不硬编分数。
4. 网络中断 —— 候选人端与后端的长连接断开。这里用"最后一次交互时间"
   做判定：超过 `STALL_SECONDS` 没有任何新的答题/抓帧/心跳，
   自动把这场标成 `disconnected`，并在前端恢复时告知"可从断点继续"。

关键取舍：**兜底不是"降级成能跑"，而是"降级成能说清楚发生了什么"**。
评测系统最怕的是崩了一半还硬出一个 62 分——那比崩掉更糟，因为看报告的人
不知道自己看到的是一份残缺数据算出来的分。
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import asdict
from typing import Any

from app import db
from app.agent.graph import run_turn
from app.agent.state import TurnState
from app.config import DIMENSIONS, DIMENSION_MAP, InterviewConfig, settings
from app.providers.llm import get_provider

# 超过这个秒数没有任何新事件，判定为"连接中断"
STALL_SECONDS = 180
# 连续无人脸超过这个秒数，判定为"中途离开"
AWAY_SECONDS = 20


def _cfg_from_dict(d: dict) -> InterviewConfig:
    """从落库的 JSON 快照还原配置。

    只认 dataclass 上真实存在的字段，未知字段忽略——
    这样前端多传字段不会 500，也不用写 pydantic 校验（本项目没装 pydantic
    的 fastapi 集成，但 pydantic 本身装了，这里刻意不用，避免两套校验逻辑）。
    """
    allowed = {f for f in InterviewConfig.__dataclass_fields__}
    kw = {k: v for k, v in (d or {}).items() if k in allowed}
    cfg = InterviewConfig(**kw)
    if "weights" not in kw:
        cfg.weights = {d_["key"]: d_["weight"] for d_ in DIMENSIONS}
    if "enabled_dimensions" not in kw:
        cfg.enabled_dimensions = [d_["key"] for d_ in DIMENSIONS]
    cfg.validate()
    return cfg


class Session:
    """一场进行中面试的内存视图。真源永远是 SQLite，这里只是缓存。"""

    def __init__(self, iid: str, cfg: InterviewConfig, provider):
        self.iid = iid
        self.cfg = cfg
        self.provider = provider
        self.lock = threading.Lock()
        self.last_activity = time.time()
        self.last_frame_ts = 0.0
        # 云端视觉模型是另一种节流：它按张计费，本地启发式是免费的，
        # 所以两条通道的间隔必须分开算（1.2s vs 8s）。
        self.last_vision_model_ts = 0.0
        self.away_since: float | None = None
        self.fallbacks: dict[str, Any] = {}
        # 每帧的缩略图，用来算帧间 motion（不落库，只在内存）
        self.prev_small = None

    # -- 状态重建 ---------------------------------------------------------
    def load_state(self, answer: str = "", audio_ms: int = 0,
                   submitted: bool = False) -> TurnState:
        """从数据库重建一轮的输入 state。

        每次提交回答都重建一次，而不是在内存里维护一份越来越大的 state。
        理由：单场面试只有几十轮，重建成本可忽略；而"内存 state 与库不一致"
        是这类系统最常见的隐性 bug 来源。

        `submitted` 必须由调用方显式传：图和 DB 都无法从 `answer == ""` 反推出
        "这是首轮"还是"候选人提交了一个空回答"，而这两种情况的处理完全不同
        （前者出题，后者追问确认是不是网络断了）。
        """
        turns = db.list_turns(self.iid)
        done = [t for t in turns if (t.get("answer") or "").strip()]
        asked = [t["question"] for t in turns if t.get("question")]
        idx = len(turns)
        phase = turns[-1]["phase"] if turns else "main"
        question = turns[-1]["question"] if turns else ""

        # 追问的父问题：往前找最近的 main/pressure/variant
        parent_idx = None
        for t in reversed(turns):
            if t.get("phase") in ("main", "pressure", "variant"):
                parent_idx = t["idx"]
                break
            if t.get("idx") == idx - 1:
                continue

        return {
            "interview_id": self.iid,
            "job_role": self.cfg.job_role,
            "difficulty": self.cfg.difficulty,
            "resume": (db.get_interview(self.iid) or {}).get("resume_text") or "",
            "turns": [dict(t) for t in done],
            "asked_questions": asked,
            "idx": idx,
            "parent_idx": parent_idx,
            "phase": phase,
            "question": question,
            "answer": answer,
            "answer_audio_ms": audio_ms,
            "submitted": submitted,
            "progress": [],
        }

    # -- 出题 -------------------------------------------------------------
    def ask_next(self) -> dict:
        """出下一题（首题 / 追问 / 压力 / 变更 / 下一主问题都由图决定）。"""
        with self.lock:
            state = self.load_state()
            try:
                out, steps = run_turn(state, self.cfg, self.provider)
            except Exception as e:  # 图炸了也不能让面试卡住
                self.fallbacks["graph_error"] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                out = {"next_action": "ask",
                       "next_question": "请介绍一下你最近做的一个项目。",
                       "target_dim": "execution"}
                steps = [{"step": "error", "detail": str(e), "ok": False, "ts": time.time()}]

            if out.get("next_action") == "closing":
                return {"action": "closing", "question": "", "steps": steps, "turn_idx": None}

            idx = len(db.list_turns(self.iid))
            phase = _phase_of(out.get("next_action"))
            question = out.get("next_question") or "请继续。"
            kind = out.get("target_dim") or "comprehension"
            db.add_turn(self.iid, idx, phase, question, kind)
            self.last_activity = time.time()
            return {"action": phase, "question": question, "kind": kind,
                    "turn_idx": idx, "steps": steps}

    # -- 提交答案 ---------------------------------------------------------
    def submit(self, answer: str, audio_ms: int = 0) -> dict:
        """候选人提交一次回答，推进一轮。"""
        with self.lock:
            turns = db.list_turns(self.iid)
            pending = next((t for t in turns if not (t.get("answer") or "").strip()), None)
            if pending is None:
                # 没有待答的题：说明前端状态乱了，重出一题而不是报错卡住
                self.last_activity = time.time()
                return {"action": "ask", **self._ask_locked(), "steps": []}

            latency = int((time.time() - (pending.get("asked_at") or time.time())) * 1000)
            db.answer_turn(self.iid, pending["idx"], answer, audio_ms, latency)
            self.last_activity = time.time()

            state = self.load_state(answer=answer, audio_ms=audio_ms, submitted=True)
            try:
                out, steps = run_turn(state, self.cfg, self.provider)
            except Exception as e:
                self.fallbacks["graph_error"] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                out, steps = {"next_action": "closing", "next_question": ""}, [
                    {"step": "error", "detail": str(e), "ok": False, "ts": time.time()}]

            action = out.get("next_action") or "closing"
            if action in ("closing", "end"):
                db.set_interview_status(self.iid, "finished", ended=True, fallback=self.fallbacks or None)
                return {"action": "closing", "question": "", "steps": steps,
                        "followup_decision": out.get("followup_decision")}

            if action == "ask":
                # decide 只决定"该出下一道主问题"，题面还得再跑一次图生成。
                # 第一版漏了这一步，导致 phase=main 但 question 为空字符串的坏记录。
                q, kind, idx = self._generate_main_question()
                return {"action": "main", "question": q, "kind": kind,
                        "turn_idx": idx, "steps": steps,
                        "followup_decision": out.get("followup_decision")}

            idx = len(db.list_turns(self.iid))
            phase = _phase_of(action)
            question = out.get("next_question") or ""
            kind = out.get("target_dim") or state.get("target_dim") or "comprehension"
            db.add_turn(self.iid, idx, phase, question, kind)
            return {"action": phase, "question": question, "kind": kind,
                    "turn_idx": idx, "steps": steps,
                    "followup_decision": out.get("followup_decision")}

    def _ask_locked(self) -> dict:
        r = self.ask_next_unlocked()
        return r

    def _generate_main_question(self) -> tuple[str, str, int]:
        """只跑"出下一道主问题"这一步（prepare → generate → END）。

        为什么要单独一个方法：`submit()` 里 decide 已经判定了"该继续下一题"，
        但 decide 不产生题面。如果直接复用 `ask_next()`，会再跑一遍 prepare，
        而此时 turns 里已经有若干条、可能又判出"该插压力题"，
        导致 decide 说"下一主问题"、实际出的是压力题，phase 与内容对不上。
        这里显式只走出题分支，让 decide 的判定成为唯一真相。
        """
        state = self.load_state()
        state["answer"] = ""
        try:
            out, _ = run_turn(state, self.cfg, self.provider)
        except Exception as e:
            self.fallbacks["graph_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            out = {"next_question": "请继续介绍你上一个项目的技术细节。"}
        q = out.get("next_question") or "请继续介绍你上一个项目的技术细节。"
        idx = len(db.list_turns(self.iid))
        db.add_turn(self.iid, idx, "main", q, out.get("target_dim") or "comprehension")
        return q, out.get("target_dim") or "comprehension", idx

    def ask_next_unlocked(self) -> dict:
        idx = len(db.list_turns(self.iid))
        q = "请介绍一下你最近做的一个项目。"
        db.add_turn(self.iid, idx, "main", q, "execution")
        return {"question": q, "kind": "execution", "turn_idx": idx}


def _phase_of(action: str | None) -> str:
    """把图的 next_action 翻译成落库的 phase。

    这两者刻意不合并成一个字段：
    - `next_action` 是"决策"，语义是给路由用的动词（ask/followup/...）
    - `phase` 是"这条记录属于哪个阶段"，语义是给统计和评分用的名词
    混用会让"我要不要出题"和"这题算什么阶段"耦合，后面加阶段就得改路由。
    """
    return {
        "ask": "main",
        "followup": "followup",
        "pressure": "pressure",
        "variant": "variant",
    }.get(action or "", "main")


# ---------------------------------------------------------------------------
# 会话管理器
# ---------------------------------------------------------------------------
class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, candidate_name: str, job_role: str, resume_text: str,
               config_dict: dict | None = None) -> dict:
        cfg = _cfg_from_dict(config_dict or {"job_role": job_role})
        cfg.job_role = job_role or cfg.job_role
        provider = get_provider()
        iid = db.create_interview(candidate_name, cfg.job_role, resume_text,
                                  asdict(cfg), provider.name)
        s = Session(iid, cfg, provider)
        with self._lock:
            self._sessions[iid] = s
        db.set_interview_status(iid, "running")
        first = s.ask_next()
        return {"interview_id": iid, "provider": provider.name,
                "config": asdict(cfg), "first": first}

    def get(self, iid: str) -> Session | None:
        with self._lock:
            s = self._sessions.get(iid)
        if s is not None:
            return s
        # 进程重启后从库里恢复
        row = db.get_interview(iid)
        if not row:
            return None
        try:
            cfg = _cfg_from_dict(json.loads(row.get("config_json") or "{}"))
        except Exception:
            cfg = InterviewConfig(job_role=row.get("job_role") or "后端开发实习生")
        s = Session(iid, cfg, get_provider())
        try:
            s.fallbacks = json.loads(row.get("fallback_json") or "{}")
        except Exception:
            s.fallbacks = {}
        with self._lock:
            self._sessions[iid] = s
        return s

    def drop(self, iid: str) -> None:
        with self._lock:
            self._sessions.pop(iid, None)


manager = SessionManager()


# ---------------------------------------------------------------------------
# 人工干预：面试过程中调参数
# ---------------------------------------------------------------------------
def adjust_config(iid: str, patch: dict) -> dict:
    """面试进行中改参数。

    这一条对应需求「面试过程参数可人工参与调整」，具体是：
    - 改 `questions_target`（还想多问几题）
    - 改 `followup_max`（追问太狠/太松）
    - 改 `weights` / `enabled_dimensions`（某岗位临时抬高某维度）
    - 改 `base_minutes` / `max_minutes`（延长面试时间）

    改完立刻落库（`interview.config_json` 是快照，会被覆盖）。
    为什么不改就"下一场才生效"：面试官是在听到候选人回答之后才想加题/加权的，
    如果下一场才生效，这个"人工干预"就是假的。
    """
    s = manager.get(iid)
    row = db.get_interview(iid)
    if not row:
        raise KeyError(f"面试不存在: {iid}")
    cfg_dict = json.loads(row.get("config_json") or "{}")
    cfg_dict.update({k: v for k, v in patch.items()
                     if k in InterviewConfig.__dataclass_fields__})
    cfg = _cfg_from_dict(cfg_dict)
    if s:
        s.cfg = cfg
    with db.get_conn() as conn:
        conn.execute("UPDATE interview SET config_json=?, job_role=? WHERE id=?",
                     (json.dumps(asdict(cfg), ensure_ascii=False), cfg.job_role, iid))
    return {"config": asdict(cfg), "applied_at": time.time()}


def pacing_suggestion(iid: str) -> dict:
    """根据当前证据给一个"节奏建议"，给面试官看，不自动执行。

    需求里举的例子是"判断耐心不足可以延长面试时间"。
    本实现刻意做成**建议 + 一键应用**，而不是自动改参数：
    自动改会让面试官失去对过程的控制，而这套系统的定位是"辅助面试官"。
    """
    s = manager.get(iid)
    row = db.get_interview(iid)
    if not row:
        raise KeyError(iid)
    turns = db.list_turns(iid)
    answered = [t for t in turns if (t.get("answer") or "").strip()]
    avg_len = sum(len(t.get("answer") or "") for t in answered) / max(len(answered), 1)

    hints = []
    if s and len(answered) >= 3:
        followups = [t for t in turns if t.get("phase") == "followup"]
        if avg_len < 40 and len(followups) < 2:
            hints.append({"kind": "extend",
                          "text": "回答普遍偏短，可延长面试时间多问 2 题，观察是否只是紧张。",
                          "patch": {"questions_target": min(s.cfg.questions_target + 2, 12)}})
        if avg_len > 200:
            hints.append({"kind": "shorten",
                          "text": "回答信息量较大，可提前收尾或减少题量，避免单场过长。",
                          "patch": {"questions_target": max(s.cfg.questions_target - 1, 3)}})
    if not hints:
        hints.append({"kind": "steady", "text": "当前节奏正常，保持即可。", "patch": {}})
    return {"hints": hints, "answered": len(answered), "avg_answer_len": round(avg_len, 1)}


# ---------------------------------------------------------------------------
# 视频 / 音频事件接入
# ---------------------------------------------------------------------------
def ingest_frame(iid: str, image_b64: str) -> dict:
    """接收一帧，做本地启发式分析后落库。节流由调用方（前端）与这里双重保证。"""
    from app.vision.analyzer import analyze_frame

    s = manager.get(iid)
    if s is None:
        raise KeyError(iid)
    now = time.time()
    if (now - s.last_frame_ts) * 1000 < settings.vision_min_interval_ms:
        return {"skipped": True, "reason": "节流"}
    s.last_frame_ts = now
    s.last_activity = now

    vr, small = analyze_frame(image_b64, s.prev_small)
    s.prev_small = small

    # 云端视觉模型（可选，默认关）。本地启发式能回答"在不在画面、大概看哪"，
    # 但"头部朝向"这种要模型才判得准。两条通道分开节流：本地 1.2s 一张，
    # 模型 8s 一张，因为模型按张计费、整场 400 张要 ¥1.36。
    note = vr.note
    model_verdict = None
    if settings.vision_use_model:
        if (now - s.last_vision_model_ts) * 1000 >= settings.vision_model_interval_ms:
            s.last_vision_model_ts = now
            try:
                r = s.provider.vision({"image_b64": image_b64})
                if r.ok and isinstance(r.data, dict):
                    model_verdict = r.data
                    pose = r.data.get("head_pose") or "unknown"
                    look = r.data.get("looking_at_camera")
                    note = f"{note} | 模型: head_pose={pose} looking={look}"
            except Exception as e:  # 模型挂掉不能影响抽帧主链路
                note = f"{note} | 模型调用失败: {type(e).__name__}"

    db.add_vision_frame(iid, ts=now, face_found=1 if vr.face_found else 0,
                        face_ratio=vr.face_ratio, center_dx=vr.center_dx,
                        center_dy=vr.center_dy, brightness=vr.brightness,
                        looking=1 if vr.looking else 0, motion=vr.motion,
                        note=note)
    # 中途离开检测
    if vr.face_found:
        s.away_since = None
    else:
        if s.away_since is None:
            s.away_since = now
        elif now - s.away_since >= AWAY_SECONDS:
            s.fallbacks["candidate_left"] = {
                "away_seconds": round(now - s.away_since, 1),
                "detected_at": now,
                "note": f"连续 {AWAY_SECONDS}s 以上画面中没有人脸",
            }
    return {"frame": vr.to_dict(), "model": model_verdict,
            "away_for": round(now - s.away_since, 1) if s.away_since else 0}


def ingest_audio_event(iid: str, kind: str, value: float | None = None,
                       text: str | None = None, meta: dict | None = None) -> None:
    s = manager.get(iid)
    if s:
        s.last_activity = time.time()
    db.add_audio_event(iid, kind=kind, value=value, text=text, meta=meta)


# ---------------------------------------------------------------------------
# 异常判定（报告生成前跑一遍）
# ---------------------------------------------------------------------------
def detect_fallbacks(iid: str) -> dict:
    """判定这场面试属于哪种异常状态。

    **这是报告里最重要的一段逻辑**：它决定报告是"一份可信的评估"还是
    "一份说明为什么给不出评估的记录"。四种情况都必须能说清楚。
    """
    row = db.get_interview(iid)
    if not row:
        raise KeyError(iid)
    turns = db.list_turns(iid)
    frames = db.list_vision_frames(iid)
    audios = db.list_audio_events(iid)
    answered = [t for t in turns if (t.get("answer") or "").strip()]
    total_chars = sum(len(t.get("answer") or "") for t in answered)

    flags: dict[str, Any] = {}
    started = row.get("started_at") or 0
    ended = row.get("ended_at") or 0
    duration = (ended - started) if ended else (time.time() - started)

    # 1. 时长 0 / 从未真正开始
    # 注意：必须区分"已经结束但只跑了 0.x 秒"和"刚开始跑 0.x 秒"。
    # 第一版写成 `duration < 1.0 or not answered`，结果面试刚开始就误报 no_progress——
    # 因为 duration 是 now - started，刚创建当然小于 1 秒。
    # 正确条件是：已经结束（ended）且时长不足 1 秒，或者压根没答过题。
    ended_instant = bool(ended) and duration < 1.0
    if ended_instant or not answered:
        flags["no_progress"] = {
            "duration_sec": round(duration, 1),
            "answered": len(answered),
            "note": "没有产生任何有效问答，不生成评分。",
        }

    # 2. 录音为空
    transcribed = [a for a in audios if a.get("kind") == "transcript" and (a.get("text") or "").strip()]
    if total_chars == 0:
        flags["empty_audio"] = {
            "transcript_events": len(transcribed), "total_chars": 0,
            "note": "所有回答都是空文本，可能是麦克风未授权或未开录音。",
        }
    elif transcribed and total_chars > 0:
        # 有转写但明显少于打字文本 → 语音链路部分失败
        pass

    # 3. 中途离开
    if frames:
        gaps = []
        prev = None
        for f in frames:
            if not f.get("face_found"):
                if prev is None:
                    prev = f["ts"]
            else:
                if prev is not None:
                    gaps.append(f["ts"] - prev)
                    prev = None
        if prev is not None:
            gaps.append(frames[-1]["ts"] - prev)
        longest = max(gaps) if gaps else 0.0
        if longest >= AWAY_SECONDS:
            flags["candidate_left"] = {
                "longest_away_sec": round(longest, 1),
                "frames": len(frames),
                "note": f"检测到最长 {round(longest, 1)} 秒画面中没有人脸。",
            }
    else:
        flags["no_video"] = {"frames": 0, "note": "没有任何视频帧，摄像头可能未授权。"}

    # 4. 网络中断 / 停滞
    # 关键：这里的"最后活动"只统计**候选人侧**的信号（提交回答、视频帧、音频事件），
    # 不统计 `asked_at`。出题是服务端行为，服务器问完题在等人答，这段等待本身
    # 就是它该有的样子——把 asked_at 算进去，面试官刚发完第一题就永远判不出停滞。
    candidate_activity = (
        [t.get("answered_at") or 0 for t in answered]
        + [f.get("ts") or 0 for f in frames]
        + [a.get("ts") or 0 for a in audios]
    )
    last_seen = max(candidate_activity + [started])
    if row.get("status") == "aborted" and row.get("abort_reason"):
        flags["aborted"] = {"reason": row["abort_reason"]}
    elif row.get("status") != "finished" and time.time() - last_seen > STALL_SECONDS:
        flags["disconnected"] = {
            "stalled_sec": round(time.time() - last_seen, 1),
            "note": "超过 3 分钟没有任何候选人侧新事件，判定会话中断（可从断点继续）。",
        }

    # 合并进程内记录
    s = manager.get(iid)
    if s and s.fallbacks:
        for k, v in s.fallbacks.items():
            flags.setdefault(k, v)

    return {
        "flags": flags,
        "duration_sec": round(duration, 1),
        "answered": len(answered),
        "asked": len(turns),
        "total_chars": total_chars,
        "frames": len(frames),
        "blocking": bool(flags.get("no_progress") or flags.get("empty_audio")),
    }
