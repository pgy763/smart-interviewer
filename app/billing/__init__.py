"""商业化层：配额、预算熔断、审计。

需求里第 7 条要「较成熟的商业化方案」。这里的判断是：**不要在 demo 里假装
自己有一个完整的 SaaS**。真正会被问到的是三件事，所以只把这三件事做实：

1. **怎么收钱** —— 按「场次」卖，不按 token 卖。
   企业 HR 能判断「一个月 200 场够不够」，判断不了「37 万 token 够不够」。
   我们内部把场次折算成成本（见 `app/providers/llm.estimate_cost`），
   这个折算是**我们的毛利问题，不是客户的问题**——这层不该暴露给客户。

2. **怎么防止一场面试把钱烧爆** —— 预算熔断。
   单场模型调用成本超过上限时，把后续调用降级到 mock（规则化），
   面试继续、报告照出，但报告里明确标注「本场有 N 次调用因预算熔断被降级」。
   **刻意不做的**：不中断面试。候选人答到一半被系统踢出去，是比多花两毛钱
   严重得多的产品事故。

3. **出了事怎么查** —— 审计日志。
   每场面试落一条：租户、时间、开始/结束、调用次数、成本、是否触发熔断、
   是否被人工终止。这是给财务和合规看的，不是给我们自己 debug 看的。

**明确不做（写在这里是为了不让人误会成漏了）**：
- 不做支付/发票/退款流程（这属于财务系统，不是面试系统）。
- 不做多租户数据隔离的强保证（现在是单库 + tenant_id 列，能查能筛；
  真要多租户，应该按租户分库或走行级安全，那是另一个量级的活）。
- 不做 SSO / 权限矩阵（现在没有登录，见 docs/ARCHITECTURE.md 的边界一节）。
"""

from app.billing.quota import (
    BudgetGuard,
    QuotaDecision,
    QuotaExceeded,
    audit,
    check_quota,
    consume,
    ensure_tenant,
    ledger_summary,
    tenant_usage,
)

__all__ = [
    "BudgetGuard",
    "QuotaDecision",
    "QuotaExceeded",
    "audit",
    "check_quota",
    "consume",
    "ensure_tenant",
    "ledger_summary",
    "tenant_usage",
]
