"""预算熔断的装饰器：把任何 provider 包成"钱花超了就降级成规则化"。

为什么用装饰器而不是在 provider 里写 if：
- provider 的职责是"怎么和模型说话"，它不知道"这一场面试还剩多少预算"；
- 预算的边界是**场**（一个 interview_id），而 provider 是无状态的单例；
- 装饰器让"花钱"这件事集中在一条链路上，将来换成 OpenAI 也一样套。

降级成什么：`MockProvider`。不是"跳过这一步"——跳过会让题面为空、
报告缺维度，那才是真的坏掉了。规则化的题目虽然朴素，但链路是完整的。
"""

from __future__ import annotations

import logging

from app.billing.quota import BudgetGuard, audit
from app.providers.llm import LLMResult, MockProvider

log = logging.getLogger(__name__)


class GuardedProvider:
    """包一层预算检查。方法签名与 providers.llm 的两个实现保持一致。"""

    def __init__(self, inner, guard: BudgetGuard, interview_id: str = "",
                 tenant: str = "default"):
        self._inner = inner
        self._guard = guard
        self._iid = interview_id
        self._tenant = tenant
        self._mock = MockProvider()
        self._reported = False

    # ---- 对外暴露的属性要"看起来像"被包的那个 provider ----
    @property
    def name(self) -> str:
        return f"{getattr(self._inner, 'name', 'unknown')}+guard"

    @property
    def guard(self) -> BudgetGuard:
        return self._guard

    def _trip(self, purpose: str) -> None:
        """第一次熔断时落一条审计。只落一条，不按调用次数刷屏。"""
        if self._reported:
            return
        self._reported = True
        audit("budget.trip", tenant=self._tenant, interview_id=self._iid,
              detail={"purpose": purpose, **self._guard.to_dict()}, outcome="denied")
        log.warning("预算熔断：interview=%s purpose=%s spent=%.4f/%.4f",
                    self._iid, purpose, self._guard.spent_cny, self._guard.limit_cny)

    def _call(self, method: str, ctx: dict, purpose: str) -> LLMResult:
        # 文本调用预估很便宜，这里按 0 估；真正的花费在返回后按 usage 记账。
        if not self._guard.allow(0.0):
            self._trip(purpose)
            res = getattr(self._mock, method)(ctx)
            # 打一个标记，报告里能说清"这条不是真模型给的"
            if isinstance(res.data, dict):
                res.data = {**res.data, "_degraded": "budget"}
            return res
        res = getattr(self._inner, method)(ctx)
        try:
            if res.ok and res.data is not None:
                # 有 usage 才能算钱；拿不到就按 0 记，至少次数是准的
                self._guard.record(self._estimate(res))
        except Exception:  # pragma: no cover - 记账失败不能影响主流程
            pass
        return res

    def _estimate(self, res: LLMResult) -> float:
        """粗估一次调用的钱。目的只是让保险丝有输入，不是精确账单。

        精确账单在 `estimate_cost()` 里按 llm_call 表算；这里为了不引入
        查库开销，用一个"文本调用约 0.0002 元"的量级常数。
        """
        if "omni" in (res.model or ""):
            return 0.02
        if "vl" in (res.model or ""):
            return 0.004
        return 0.0002

    # ---- 逐个转发 ----
    def question(self, ctx: dict) -> LLMResult:
        return self._call("question", ctx, "question")

    def followup(self, ctx: dict) -> LLMResult:
        return self._call("followup", ctx, "followup")

    def pressure(self, ctx: dict) -> LLMResult:
        return self._call("pressure", ctx, "pressure")

    def variant(self, ctx: dict) -> LLMResult:
        return self._call("variant", ctx, "variant")

    def analyze_answer(self, ctx: dict) -> LLMResult:
        return self._call("analyze_answer", ctx, "score")

    def vision(self, ctx: dict) -> LLMResult:
        return self._call("vision", ctx, "vision")

    def resume_digest(self, ctx: dict) -> LLMResult:
        return self._call("resume_digest", ctx, "resume_digest")
