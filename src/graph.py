"""V0 的 LangGraph 状态图：接线层，不含任何业务判断。

图结构（与 system-design/protocols/multi_hop_qa.json 的 edges 一致）：

    START → planner_v0 → retriever_v0 → answer_validator_v0
                                          ├─ verified            → END
                                          ├─ insufficient（<3 次）→ retriever_v0
                                          └─ conflict（<2 次）    → planner_v0

职责边界：
- 本模块只负责"接线"：注册节点、固定边、把 route_after_validator 挂成条件边；
- 路由判定全部在 src/router.py，本模块不复制任何判断逻辑；
- nodes / checkpointer 均可注入，测试用 fake 节点，全程离线、零 API 成本。
"""

from __future__ import annotations

from functools import partial
from typing import Any, Callable, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph

from src.agents.answer_validator import answer_validator_fn
from src.agents.planner import planner_fn
from src.agents.retriever import retriever_fn
from src.llm_usage import LLMUsageCollector
from src.router import PLANNER_NODE, RETRIEVER_NODE, route_after_validator
from src.state import AgentState

ANSWER_VALIDATOR_NODE = "answer_validator_v0"

NodeFn = Callable[[AgentState], dict[str, Any]]


def default_nodes(
    usage_collector: LLMUsageCollector | None = None,
) -> dict[str, NodeFn]:
    """真实节点实现：三个 LangGraph 节点函数。"""
    return {
        PLANNER_NODE: partial(planner_fn, usage_collector=usage_collector),
        RETRIEVER_NODE: retriever_fn,
        ANSWER_VALIDATOR_NODE: partial(
            answer_validator_fn, usage_collector=usage_collector
        ),
    }


def build_graph(
    nodes: Optional[dict[str, NodeFn]] = None,
    checkpointer: Optional[Any] = None,
):
    """构建并编译 V0 状态图。

    nodes 为 None 时使用真实节点；传入 fake 节点即可离线验证图与路由。
    checkpointer 默认为 InMemorySaver（内存级，支持 thread_id 多轮调试）。
    """
    impl = nodes if nodes is not None else default_nodes()

    graph = StateGraph(AgentState)

    graph.add_node(PLANNER_NODE, impl[PLANNER_NODE])
    graph.add_node(RETRIEVER_NODE, impl[RETRIEVER_NODE])
    graph.add_node(ANSWER_VALIDATOR_NODE, impl[ANSWER_VALIDATOR_NODE])

    graph.set_entry_point(PLANNER_NODE)
    graph.add_edge(PLANNER_NODE, RETRIEVER_NODE)
    graph.add_edge(RETRIEVER_NODE, ANSWER_VALIDATOR_NODE)

    graph.add_conditional_edges(
        ANSWER_VALIDATOR_NODE,
        route_after_validator,
        {
            RETRIEVER_NODE: RETRIEVER_NODE,
            PLANNER_NODE: PLANNER_NODE,
            END: END,
        },
    )

    return graph.compile(
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver()
    )
