"""SQLite 存储层。

为什么用 SQLite 而不是 MySQL/Postgres：
demo 要能"一条命令跑起来"，评委/面试官 git clone 完不该先装数据库。
SQLite 是单文件、零运维，且支持并发读；写入都是单条 upsert，量级在
一场面试几百条事件，完全够。真要上线换成 Postgres 时，只需要换
`connect()` 与少量 SQL 方言（本文件已把方言集中写在 schema 里）。

关键设计：**所有写入都走 append-only 的事件表 + 少量快照表**。
面试过程是时间序列，事件表能保证"即使进程崩了也能复盘到最后一条"。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from app.config import settings

log = logging.getLogger(__name__)
_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 一场面试
CREATE TABLE IF NOT EXISTS interview (
    id              TEXT PRIMARY KEY,
    candidate_name  TEXT NOT NULL,
    job_role        TEXT NOT NULL,
    resume_text     TEXT,
    config_json     TEXT NOT NULL,       -- InterviewConfig 的 JSON 快照
    status          TEXT NOT NULL,       -- created | running | finished | aborted
    provider        TEXT NOT NULL,
    started_at      REAL NOT NULL,
    ended_at        REAL,
    abort_reason    TEXT,
    -- 四种异常的兜底结果记在这里，报告里要如实展示
    fallback_json   TEXT,
    -- 接入层产物：结构化简历档案 / 上一轮报告摘要 / 素材来源
    -- 存 JSON 而不是拆成列，是因为这两样是"外部物料的解析快照"，
    -- 结构会随解析规则演进而变，拆列会让每次改解析都要改表。
    resume_profile_json TEXT,
    prior_report_json   TEXT,
    material_json       TEXT
);

-- 面试过程中每一轮问答
CREATE TABLE IF NOT EXISTS turn (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id    TEXT NOT NULL REFERENCES interview(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,    -- 第几轮，从 0 开始
    phase           TEXT NOT NULL,       -- main | followup | pressure | variant | closing
    question        TEXT NOT NULL,
    question_kind   TEXT,                -- 该题想考察的维度
    answer          TEXT,
    answer_audio_ms INTEGER DEFAULT 0,
    asked_at        REAL NOT NULL,
    answered_at     REAL,
    latency_ms      INTEGER,             -- 从提问到开答的间隔
    UNIQUE(interview_id, idx)
);

-- 每一帧视频分析结果（节流后）
CREATE TABLE IF NOT EXISTS vision_frame (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id    TEXT NOT NULL REFERENCES interview(id) ON DELETE CASCADE,
    ts              REAL NOT NULL,
    face_found      INTEGER NOT NULL,
    face_ratio      REAL,                -- 人脸框面积占画面比例
    center_dx       REAL,                -- 人脸中心相对画面中心的水平偏移，-1..1
    center_dy       REAL,
    brightness      REAL,
    looking         INTEGER,             -- 是否判定为"看镜头"
    motion          REAL,                -- 帧间差异，粗略衡量肢体动作幅度
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_vision_interview ON vision_frame(interview_id, ts);

-- 音频事件（转写片段、停顿、语速）
CREATE TABLE IF NOT EXISTS audio_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id    TEXT NOT NULL REFERENCES interview(id) ON DELETE CASCADE,
    ts              REAL NOT NULL,
    kind            TEXT NOT NULL,       -- transcript | pause | speed | filler
    value           REAL,
    text            TEXT,
    meta_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audio_interview ON audio_event(interview_id, ts);

-- 模型调用账本：成本核算的唯一数据源
CREATE TABLE IF NOT EXISTS llm_call (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id    TEXT,
    ts              REAL NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    purpose         TEXT NOT NULL,       -- question | score | followup | vision_judge | report | stt
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    audio_ms        INTEGER DEFAULT 0,
    latency_ms      INTEGER,
    ok              INTEGER NOT NULL DEFAULT 1,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_interview ON llm_call(interview_id);

-- 最终评分（每维度一行，方便报告直接查）
CREATE TABLE IF NOT EXISTS dimension_score (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id    TEXT NOT NULL REFERENCES interview(id) ON DELETE CASCADE,
    dim_key         TEXT NOT NULL,
    score           REAL,                -- NULL 表示证据不足
    status          TEXT NOT NULL,       -- scored | insufficient_evidence
    reason          TEXT,
    evidence_json   TEXT,
    UNIQUE(interview_id, dim_key)
);
"""


def _connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


# 加列迁移表：{表名: [(列名, 列定义)]}
# 为什么需要它：SCHEMA 里全是 `CREATE TABLE IF NOT EXISTS`，对**已存在**的表
# 是空操作。老库升级上来时新列不存在，插入/查询会报 `no such column`。
# 这和 im 项目里踩的坑是同一个（那里是 MySQL 的 SchemaUpgrade）。
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "interview": [
        ("resume_profile_json", "TEXT"),
        ("prior_report_json", "TEXT"),
        ("material_json", "TEXT"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等加列：先查 PRAGMA table_info，缺哪列补哪列。

    幂等性来自"查询"而不是"捕获异常"——捕获 `duplicate column` 也行，
    但那样每次启动都会在日志里留一条错误，运维看日志时会以为是故障。
    """
    for table, cols in _MIGRATIONS.items():
        try:
            have = _columns(conn, table)
        except sqlite3.Error:
            continue  # 表还没建（首次启动的中间态），SCHEMA 会负责建
        for name, decl in cols:
            if name in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("表结构升级：%s.%s 已添加", table, name)
            except sqlite3.Error as e:  # pragma: no cover - 并发启动时可能撞
                log.error("表结构升级失败：%s.%s：%s", table, name, e)


def init_db(path: Path | str | None = None) -> None:
    p = Path(path or settings.db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _connect(p) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def get_conn(path: Path | str | None = None):
    init_db(path)
    conn = _connect(path or settings.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def new_id() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# interview
# ---------------------------------------------------------------------------
def create_interview(
    candidate_name: str,
    job_role: str,
    resume_text: str,
    config_json: dict,
    provider: str,
    resume_profile: dict | None = None,
    prior_report: dict | None = None,
    material: dict | None = None,
) -> str:
    iid = new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO interview
               (id, candidate_name, job_role, resume_text, config_json, status, provider, started_at,
                resume_profile_json, prior_report_json, material_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (iid, candidate_name, job_role, resume_text, json.dumps(config_json, ensure_ascii=False),
             "created", provider, time.time(),
             json.dumps(resume_profile or {}, ensure_ascii=False),
             json.dumps(prior_report or {}, ensure_ascii=False),
             json.dumps(material or {}, ensure_ascii=False)),
        )
    return iid


def get_interview(iid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM interview WHERE id=?", (iid,)).fetchone()
    return dict(row) if row else None


def get_material(iid: str) -> dict:
    """取接入层产物，一次返回三块。

    统一在这里做 JSON 解析与容错：库里存的是文本，解析失败就返回空 dict，
    调用方（出题节点、报告）永远不用写 try/except。
    """
    row = get_interview(iid)
    if not row:
        return {"resume_profile": {}, "prior_report": {}, "material": {}}
    out: dict[str, dict] = {}
    for key, col in (("resume_profile", "resume_profile_json"),
                     ("prior_report", "prior_report_json"),
                     ("material", "material_json")):
        try:
            raw = row.get(col)
            out[key] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            out[key] = {}
    return out


def update_material(iid: str, **patch: Any) -> None:
    """面试进行中更新接入层材料（重传简历 / 粘贴上一轮报告）。

    支持四个键：
    - `resume_profile` / `prior_report`：整块替换（JSON）
    - `material`：整块替换（记录"这份材料从哪来"）
    - `resume_text`：纯文本简历原文也允许改，因为出题 fallback 要用它

    只更新传进来的键，没传的保持原值。这是刻意的：重传简历不应该顺手把
    上一轮报告清空，反过来也一样。
    """
    cols: list[str] = []
    vals: list[Any] = []
    for key, col in (("resume_profile", "resume_profile_json"),
                     ("prior_report", "prior_report_json"),
                     ("material", "material_json")):
        if key in patch:
            cols.append(f"{col}=?")
            vals.append(json.dumps(patch[key] or {}, ensure_ascii=False))
    if "resume_text" in patch:
        cols.append("resume_text=?")
        vals.append(patch["resume_text"] or "")
    if not cols:
        return
    vals.append(iid)
    with get_conn() as conn:
        conn.execute(f"UPDATE interview SET {', '.join(cols)} WHERE id=?", vals)


def list_interviews(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM interview ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_interview_status(iid: str, status: str, ended: bool = False,
                        abort_reason: str | None = None,
                        fallback: dict | None = None) -> None:
    with get_conn() as conn:
        if ended:
            conn.execute(
                "UPDATE interview SET status=?, ended_at=?, abort_reason=?, fallback_json=? WHERE id=?",
                (status, time.time(), abort_reason,
                 json.dumps(fallback, ensure_ascii=False) if fallback else None, iid),
            )
        else:
            conn.execute("UPDATE interview SET status=? WHERE id=?", (status, iid))


# ---------------------------------------------------------------------------
# turn
# ---------------------------------------------------------------------------
def add_turn(iid: str, idx: int, phase: str, question: str, question_kind: str | None,
             asked_at: float | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO turn (interview_id, idx, phase, question, question_kind, asked_at)
               VALUES (?,?,?,?,?,?)""",
            (iid, idx, phase, question, question_kind, asked_at or time.time()),
        )


def answer_turn(iid: str, idx: int, answer: str, audio_ms: int = 0,
                latency_ms: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE turn SET answer=?, answer_audio_ms=?, answered_at=?, latency_ms=?
               WHERE interview_id=? AND idx=?""",
            (answer, audio_ms, time.time(), latency_ms, iid, idx),
        )


def list_turns(iid: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM turn WHERE interview_id=? ORDER BY idx", (iid,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# vision / audio
# ---------------------------------------------------------------------------
def add_vision_frame(iid: str, **kw: Any) -> None:
    cols = ("ts", "face_found", "face_ratio", "center_dx", "center_dy",
            "brightness", "looking", "motion", "note")
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO vision_frame (interview_id,{','.join(cols)}) "
            f"VALUES (?{',?' * len(cols)})",
            (iid, *[kw.get(c) for c in cols]),
        )


def list_vision_frames(iid: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM vision_frame WHERE interview_id=? ORDER BY ts", (iid,)
        ).fetchall()
    return [dict(r) for r in rows]


def add_audio_event(iid: str, kind: str, value: float | None = None,
                    text: str | None = None, meta: dict | None = None,
                    ts: float | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audio_event (interview_id, ts, kind, value, text, meta_json) VALUES (?,?,?,?,?,?)",
            (iid, ts or time.time(), kind, value, text,
             json.dumps(meta, ensure_ascii=False) if meta else None),
        )


def list_audio_events(iid: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audio_event WHERE interview_id=? ORDER BY ts", (iid,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# llm ledger
# ---------------------------------------------------------------------------
def log_llm_call(interview_id: str | None, provider: str, model: str, purpose: str,
                 input_tokens: int = 0, output_tokens: int = 0, audio_ms: int = 0,
                 latency_ms: int = 0, ok: bool = True, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO llm_call
               (interview_id, ts, provider, model, purpose, input_tokens, output_tokens,
                audio_ms, latency_ms, ok, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (interview_id, time.time(), provider, model, purpose,
             input_tokens, output_tokens, audio_ms, latency_ms, 1 if ok else 0, error),
        )


def list_llm_calls(interview_id: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if interview_id:
            rows = conn.execute(
                "SELECT * FROM llm_call WHERE interview_id=? ORDER BY ts", (interview_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM llm_call ORDER BY ts").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# scores
# ---------------------------------------------------------------------------
def save_dimension_scores(iid: str, scores: Iterable[dict]) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM dimension_score WHERE interview_id=?", (iid,))
        for s in scores:
            conn.execute(
                """INSERT INTO dimension_score
                   (interview_id, dim_key, score, status, reason, evidence_json)
                   VALUES (?,?,?,?,?,?)""",
                (iid, s["key"], s.get("score"), s["status"], s.get("reason"),
                 json.dumps(s.get("evidence", []), ensure_ascii=False)),
            )


def list_dimension_scores(iid: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM dimension_score WHERE interview_id=? ORDER BY id", (iid,)
        ).fetchall()
    return [dict(r) for r in rows]
