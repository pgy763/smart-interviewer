"""配额 / 预算熔断 / 审计 的实现。

设计上的三条硬约束（写进代码而不是只写在文档里）：

1. **配额判断要能在没有登录系统时工作。** 现在没有用户认证，所以租户 id 由调用方
   传进来（前端从 localStorage 取一个固定值 `default`）。这不是"安全的实现"，
   而是"先在结构上留出位置、等有登录时只换取值来源"。这一点在
   docs/ARCHITECTURE.md 的「商业化边界」里说清楚了，不假装它是安全的。
2. **预算熔断必须只降级、不中断。** 见 `BudgetGuard`。
3. **审计写入绝不能抛异常。** 审计是旁路，写失败不能影响面试主流程；
   但也不能静默——写失败要落一条 warning 日志，否则财务对不上账时无从查起。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from app import db

log = logging.getLogger(__name__)

DEFAULT_TENANT = "default"


class QuotaExceeded(Exception):
    """配额用尽。调用方应该返回 402/429 并给一句人话，而不是 500。"""

    def __init__(self, message: str, *, used: int = 0, limit: int = 0):
        super().__init__(message)
        self.message = message
        self.used = used
        self.limit = limit


@dataclass
class QuotaDecision:
    """一次"能不能开新面试"的判断结果。

    `allowed=True` 时 `remaining` 是这单之后还剩多少场；
    `allowed=False` 时 `reason` 是给用户看的原话。
    """

    allowed: bool
    tenant: str
    plan: str
    used: int
    limit: int
    remaining: int
    reason: str = ""
    period: str = ""


@dataclass
class BudgetGuard:
    """单场面试的成本保险丝。

    用法：每次要花真钱调模型之前问一句 `guard.allow()`，false 就降级到 mock。
    为什么不直接在 provider 里判断：provider 不知道"这一场"的语义，
    它只知道自己被调用了一次；预算的边界是"场"，所以由会话层持有。
    """

    limit_cny: float = 1.0
    spent_cny: float = 0.0
    tripped: int = 0          # 被熔断降级的调用次数，要写进报告
    _reported: bool = field(default=False, repr=False)

    def allow(self, estimate: float = 0.0) -> bool:
        """estimate 是本次调用的预估花费（文本调用可以给 0，按调用次数兜底）。"""
        if self.spent_cny + estimate <= self.limit_cny:
            return True
        self.tripped += 1
        return False

    def record(self, cny: float) -> None:
        self.spent_cny += max(0.0, cny)

    def to_dict(self) -> dict:
        return {
            "limit_cny": round(self.limit_cny, 4),
            "spent_cny": round(self.spent_cny, 4),
            "tripped": self.tripped,
            "exhausted": self.spent_cny >= self.limit_cny,
        }


# ---------------------------------------------------------------------------
# 存储：直接用 SQLite 的同一套连接，不引入外部依赖
# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS tenant (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    plan         TEXT NOT NULL DEFAULT 'trial',
    monthly_limit INTEGER NOT NULL DEFAULT 20,
    budget_cny   REAL NOT NULL DEFAULT 1.0,
    created_at   REAL NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS tenant_usage (
    tenant    TEXT NOT NULL,
    period    TEXT NOT NULL,          -- YYYY-MM
    used      INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (tenant, period)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    tenant      TEXT NOT NULL,
    action      TEXT NOT NULL,        -- interview.create / interview.finish / budget.trip ...
    interview_id TEXT,
    actor       TEXT,
    detail_json TEXT,
    outcome     TEXT                  -- ok / denied / error
);
"""

_inited = False


def _ensure_tables() -> None:
    """建表。幂等，且只在第一次 import 后的第一次调用里跑一次。

    为什么不在 app/db.py 的 SCHEMA 里：商业化表与面试域表是两块业务，
    放在一起会让 db.py 越来越像一个杂物间；而且将来真拆库时，这两块
    也要分开搬。
    """
    global _inited
    if _inited:
        return
    with db.get_conn() as conn:
        conn.executescript(_DDL)
    _inited = True


def _period(ts: float | None = None) -> str:
    return time.strftime("%Y-%m", time.localtime(ts or time.time()))


def ensure_tenant(tenant: str, name: str = "", plan: str = "trial",
                  monthly_limit: int = 20, budget_cny: float = 1.0) -> dict:
    _ensure_tables()
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM tenant WHERE id=?", (tenant,)).fetchone()
        if row:
            return dict(row)
        conn.execute(
            "INSERT INTO tenant (id, name, plan, monthly_limit, budget_cny, created_at, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (tenant, name or tenant, plan, monthly_limit, budget_cny, time.time(),
             "自动创建：调用方第一次带这个 tenant 时补出来的"),
        )
        row = conn.execute("SELECT * FROM tenant WHERE id=?", (tenant,)).fetchone()
    return dict(row)


def tenant_usage(tenant: str, period: str | None = None) -> int:
    _ensure_tables()
    p = period or _period()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT used FROM tenant_usage WHERE tenant=? AND period=?", (tenant, p)
        ).fetchone()
    return int(row["used"]) if row else 0


def check_quota(tenant: str = DEFAULT_TENANT) -> QuotaDecision:
    """能不能再开一场。不写库（幂等、可反复调）。"""
    t = ensure_tenant(tenant)
    p = _period()
    used = tenant_usage(tenant, p)
    limit = int(t.get("monthly_limit") or 0)
    remaining = max(0, limit - used)
    allowed = used < limit
    return QuotaDecision(
        allowed=allowed,
        tenant=tenant,
        plan=str(t.get("plan") or "trial"),
        used=used,
        limit=limit,
        remaining=remaining if allowed else 0,
        reason="" if allowed else f"本月 {p} 已用 {used}/{limit} 场，额度用尽",
        period=p,
    )


def consume(tenant: str = DEFAULT_TENANT, n: int = 1) -> int:
    """占用配额。返回占用后的已用场次。

    用 UPSERT 而不是先 SELECT 再 UPDATE：并发下先查后写会超卖。
    这里用 `ON CONFLICT DO UPDATE SET used = used + ?` 让数据库保证原子性。
    """
    _ensure_tables()
    p = _period()
    ensure_tenant(tenant)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO tenant_usage (tenant, period, used, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(tenant, period) DO UPDATE SET"
            "   used = used + excluded.used, updated_at = excluded.updated_at",
            (tenant, p, n, time.time()),
        )
        row = conn.execute(
            "SELECT used FROM tenant_usage WHERE tenant=? AND period=?", (tenant, p)
        ).fetchone()
    return int(row["used"]) if row else n


def audit(action: str, *, tenant: str = DEFAULT_TENANT, interview_id: str | None = None,
          actor: str = "", detail: dict | None = None, outcome: str = "ok") -> None:
    """写一条审计。**永不抛异常**——但失败要留日志，不能静默。"""
    try:
        _ensure_tables()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, tenant, action, interview_id, actor,"
                " detail_json, outcome) VALUES (?,?,?,?,?,?,?)",
                (time.time(), tenant, action, interview_id, actor,
                 json.dumps(detail or {}, ensure_ascii=False), outcome),
            )
    except Exception as e:  # pragma: no cover - 只有库坏了才走这里
        log.warning("audit 写入失败 action=%s tenant=%s err=%s", action, tenant, e)


def list_audit(tenant: str | None = None, limit: int = 50) -> list[dict]:
    _ensure_tables()
    with db.get_conn() as conn:
        if tenant:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE tenant=? ORDER BY id DESC LIMIT ?",
                (tenant, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.get("detail_json") or "{}")
        except ValueError:
            d["detail"] = {}
        out.append(d)
    return out


def ledger_summary(tenant: str = DEFAULT_TENANT, limit: int = 500) -> dict:
    """把 llm_call 账本按用途/模型汇总，给成本页用。

    与 `estimate_cost()` 的区别：那个算「一场面试」，这个算「这个租户最近 N 条」。
    两个都留着，因为它们回答的是不同的问题。
    """
    from app.providers.llm import estimate_cost

    calls = db.list_llm_calls(None)
    calls = calls[:limit]
    cost = estimate_cost(calls)
    q = check_quota(tenant)
    return {
        "tenant": tenant,
        "quota": {
            "plan": q.plan, "used": q.used, "limit": q.limit,
            "remaining": q.remaining, "period": q.period,
        },
        "cost": cost,
        "recent_calls": len(calls),
    }
