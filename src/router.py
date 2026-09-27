"""Choose the next node from an immutable state and a validated workflow."""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END

from src.config_loader import load_protocol_config
from src.runtime.specs import WorkflowSpec
from src.state import AgentState

# 与协议 routing_rules.max_cycles 保持一致
_DEFAULT_LIMITS = load_protocol_config()["routing_rules"]["max_cycles"]
MAX_RETRIEVAL_RETRY = int(_DEFAULT_LIMITS["retrieval_retry_count"])
MAX_PLAN_RETRY = int(_DEFAULT_LIMITS["plan_retry_count"])

PLANNER_NODE = "planner_v0"
RETRIEVER_NODE = "retriever_v0"


@dataclass(frozen=True)
class RouteDecision:
    """可同时供 LangGraph 路由和运行轨迹使用的确定性决策。"""

    target: str
    reason: str


def decide_route(
    state: AgentState, workflow: WorkflowSpec | None = None,
    *, source: str = "answer_validator_v0",
) -> RouteDecision:
    """Return a deterministic target and reason without changing state."""
    if workflow is not None:
        edges = [edge for edge in workflow.edges if edge.source == source]
        if len(edges) == 1 and edges[0].route_name == "always":
            target = END if edges[0].target == "END" else edges[0].target
            return RouteDecision(target=target, reason="always")
        limits = workflow.max_cycles
        targets = {
            edge.route_name: END if edge.target == "END" else edge.target
            for edge in edges
        }
    else:
        limits = {"retrieval_retry_count": MAX_RETRIEVAL_RETRY,
                  "plan_retry_count": MAX_PLAN_RETRY}
        targets = {"verified": END, "insufficient_retry": RETRIEVER_NODE,
                   "conflict_retry": PLANNER_NODE, "fallback": END}

    # A verified answer wins even if an old failure_type remains in state.
    if state.get("verified", False):
        return RouteDecision(target=targets.get("verified", targets["fallback"]), reason="verified")

    failure_type = state.get("failure_type", "none")

    # e3_insufficient：证据不足，且未超过补充检索上限
    if failure_type == "insufficient":
        if "insufficient_retry" in targets and int(state.get("retrieval_retry_count", 0)) < limits["retrieval_retry_count"]:
            return RouteDecision(
                target=targets["insufficient_retry"], reason="insufficient_retry"
            )
        return RouteDecision(target=targets["fallback"], reason="insufficient_max_retry")

    # e3_conflict：证据冲突，且未超过重规划上限
    if failure_type == "conflict":
        if "conflict_retry" in targets and int(state.get("plan_retry_count", 0)) < limits["plan_retry_count"]:
            return RouteDecision(target=targets["conflict_retry"], reason="conflict_replan")
        return RouteDecision(target=targets["fallback"], reason="conflict_max_retry")

    # failure_type 为 none（但未 verified）或未知取值：直接结束
    return RouteDecision(target=targets["fallback"], reason="terminal_fallback")


def route_after_validator(
    state: AgentState, workflow: WorkflowSpec | None = None,
    *, source: str = "answer_validator_v0",
) -> str:
    """LangGraph 条件边适配器；实际判断统一委托给 decide_route()。"""

    return decide_route(state, workflow, source=source).target
