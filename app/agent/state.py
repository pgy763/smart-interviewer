"""LangGraph 状态定义。

**两个 schema 的分工**（这点必须在面试里讲清楚，很多人会混）：

- `TurnState`：一轮问答的状态。进图 → 出图，是纯函数。
- `InterviewRuntime`：不参与图状态合并的外部依赖（provider、数据库写入器）。

为什么不把整场面试做成一个长图、用 interrupt 挂起等答案：
一个 20 分钟的面试如果整图跑着等，进程重启、网络抖动都会让图挂住，
还要引入 checkpointer 做持久化。本项目改成 **每提交一次回答，跑一次图**，
状态由会话层持有并落库。好处是：
1. 图是纯函数，可以单元测试、可以重放；
2. 进程崩了，从数据库恢复 state 再继续就行，不需要 checkpoint 机制；
3. 每一步的输入输出都能落库，复盘时能逐步看。
代价是"跨轮的整体规划"要在 state 里显式维护（见 plan_remaining / phase_counts），
不能靠图的隐式上下文——但这本来也应该显式。
"""

from __future__ import annotations

from typing import Any, TypedDict


class TurnState(TypedDict, total=False):
    """一轮问答的完整状态。所有节点都读它、返回它的增量。"""

    # ---- 身份 ----
    interview_id: str
    job_role: str
    difficulty: str
    resume: str                # 简历原文或手填要点（可能是长文本，节点里会截断）
    resume_profile: dict       # 结构化简历档案（技能/项目/年限），见 app/intake/resume.py
    prior_report: dict         # 上一轮报告摘要（缺口维度/风险/建议追问点），见 app/intake/report.py

    # ---- 历史 ----
    turns: list[dict]          # 已完成轮次（含 phase/answer/idx）
    asked_questions: list[str]

    # ---- 本轮输入 ----
    idx: int                   # 即将产生的轮次序号
    phase: str                 # main | followup | pressure | variant | closing
    question: str              # 本轮问题（问之前由 generate 节点填）
    answer: str                # 候选人回答（前端提交时传入）
    answer_audio_ms: int
    submitted: bool            # 本轮是否"收到了提交"。空回答也是提交，必须与首轮区分开
                               # （不然"空回答 → 追问确认是否网络问题"这段逻辑永远走不到）

    # ---- 分析产物 ----
    analysis: dict             # analyze_answer 节点的输出
    followup_decision: dict    # should_followup / followup / gap
    target_dim: str            # 下一题要考察的维度

    # ---- 规划 ----
    plan: dict                 # 剩余轮次规划（剩余主问题数、是否已用压力题等）
    pace: dict                 # 节奏决策：是否延长/收尾/插入压力题

    # ---- 输出 ----
    next_action: str           # ask | followup | pressure | variant | closing | end
    next_question: str
    progress: list[dict]       # 给前端 SSE 的步骤流
    error: str | None


class InterviewRuntime(TypedDict, total=False):
    """不参与状态合并的依赖（对应 LangGraph 的 context_schema）。"""
    provider: Any
    config: Any
