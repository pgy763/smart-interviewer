"""LangGraph 装配。

图的形状：

    START → prepare → generate → analyze → decide ─┬→ (next_action=ask)     → END
                                                   ├→ (next_action=followup)  → END
                                                   ├→ (next_action=pressure)  → END
                                                   ├→ (next_action=variant)   → END
                                                   └→ (next_action=closing)   → closing → END

注意这里**不是**"跑完整场面试"。一次 `run_turn` 只跑一轮：出下一题 → 结束。
真正的问答循环在会话层（`app/api/session.py`）：候选人提交答案后，
把答案塞进 state 再跑一次图，图返回"下一题是什么 / 是不是该追问 / 该收尾了吗"。

为什么这么切分（面试里会被问）：
- 如果整场面试是一个长图 + interrupt 等答案，那么"图在运行中"这个状态必须持久化，
  要上 checkpointer（本项目连 SQLAlchemy 都没装，只有 sqlite3），
  而且进程重启后恢复图的中间态很容易出错。
- 拆成每轮一次之后，图变成纯函数 f(state) -> new_state，
  可以单元测试、可以拿历史答案重放（报告复算）、可以并发（每场面试独立）。
- 代价：跨轮的规划信息必须显式放进 state（`turns` / `plan`），
  不能依赖图的隐式上下文。这个代价是值得的，因为它让"为什么又问了一轮"可解释、可审计。

两种出题路径：
- 首轮：state 里没有 question，直接走 generate（出第一题）
- 后续轮：候选人提交答案 → analyze → decide → 得到 next_question
同一个图，靠 state 里有没有 `answer` 来区分，避免维护两条代码路径。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    node_analyze,
    node_closing,
    node_decide,
    node_generate,
    node_prepare,
)
from app.agent.state import InterviewRuntime, TurnState

_compiled = None


def _route(state: TurnState) -> str:
    """decide 之后的路由。

    除了 closing 之外，所有分支都直接 END —— 因为"出下一题"这件事
    已经由 decide/generate 写进 state.next_question 了。
    不需要再多跑一轮 generate，否则会浪费一次模型调用
    （也就浪费一次钱，成本核算是验收项之一）。
    """
    if (state.get("next_action") or "") == "closing":
        return "closing"
    return END


def build_graph():
    """编译一次，进程内复用。"""
    global _compiled
    if _compiled is not None:
        return _compiled

    g = StateGraph(TurnState, context_schema=InterviewRuntime)
    g.add_node("prepare", node_prepare)
    g.add_node("generate", node_generate)
    g.add_node("analyze", node_analyze)
    g.add_node("decide", node_decide)
    g.add_node("closing", node_closing)

    g.add_edge(START, "prepare")

    # 首轮（还没提交过回答）直接出题；后续轮先分析再决策。
    #
    # 这里**不能**用 `not state["answer"].strip()` 来判断，因为"候选人提交了一个
    # 空回答"（麦克风没录到、网络断了、直接点提交）和"这是第一轮、还没有回答"
    # 在 state 里长得一模一样。用 answer 是否为空来判断，会把空回答送进
    # generate 分支直接出下一题，decide 里"空回答 → 追问确认是否网络问题"
    # 那段逻辑永远走不到。所以显式加一个 `submitted` 标记。
    g.add_conditional_edges(
        "prepare",
        lambda s: "analyze" if s.get("submitted") else "generate",
        {"generate": "generate", "analyze": "analyze"},
    )
    g.add_edge("generate", END)
    g.add_edge("analyze", "decide")
    g.add_conditional_edges(
        "decide",
        _route,
        {"closing": "closing", END: END},
    )
    g.add_edge("closing", END)

    _compiled = g.compile()
    return _compiled


def run_turn(state: TurnState, config, provider) -> tuple[TurnState, list[dict]]:
    """跑一轮，返回 (新的 state, 本轮进度流)。

    为什么自己拼 progress：节点返回的 `progress` 是 list，
    LangGraph 默认对 list 是**覆盖**不是追加。用 `Annotated[list, operator.add]`
    可以改成追加，但那样"整场面试的进度"就得全塞进 state，
    状态会越来越胖。本项目让节点只返回自己那一条，
    调用方负责累积，state 里只留本轮。
    """
    graph = build_graph()
    init = dict(state)
    init["progress"] = []
    out = graph.invoke(init, context={"provider": provider, "config": config})
    steps = out.get("progress") or []
    return out, steps


async def astream_turn(state: TurnState, config, provider):
    """流式版本：每跑完一个节点就 yield 一次进度，给前端 SSE / WebSocket 用。

    `stream_mode="custom"` 只透出节点里 `get_stream_writer()` 写的东西。
    本项目节点没写 writer，所以这里退化成"按节点名 yield"，
    前端看到的是实质上的步骤条，不是逐 token 流。
    不做逐 token 流是刻意的：面试场景里逐字蹦出来的问题更像机器，
    而且模型输出要先 JSON 解析才能拿到干净的问题文本。
    """
    graph = build_graph()
    init = dict(state)
    init["progress"] = []
    async for chunk in graph.astream(
        init, context={"provider": provider, "config": config}, stream_mode="values"
    ):
        node_steps = chunk.get("progress") or []
        if node_steps:
            yield node_steps[-1]
