"""条件路由测试：纯函数，离线、零成本、不调用 LLM。"""

from langgraph.graph import END

from src.config_loader import load_agent_configs
from src.router import (
    MAX_PLAN_RETRY,
    MAX_RETRIEVAL_RETRY,
    PLANNER_NODE,
    RETRIEVER_NODE,
    decide_route,
    route_after_validator,
)


def test_verified_goes_to_end():
    assert route_after_validator({"question": "q", "verified": True}) == END


def test_verified_wins_over_stale_failure_type():
    """验证通过优先：即使 failure_type 残留旧值也不再触发重试。"""
    assert route_after_validator({"verified": True, "failure_type": "conflict"}) == END


def test_insufficient_retries_retriever():
    state = {"failure_type": "insufficient", "retrieval_retry_count": 0}
    assert route_after_validator(state) == RETRIEVER_NODE


def test_insufficient_stops_at_max_retry():
    state = {"failure_type": "insufficient", "retrieval_retry_count": MAX_RETRIEVAL_RETRY}
    assert route_after_validator(state) == END


def test_conflict_replans():
    state = {"failure_type": "conflict", "plan_retry_count": 0}
    assert route_after_validator(state) == PLANNER_NODE


def test_conflict_stops_at_max_retry():
    state = {"failure_type": "conflict", "plan_retry_count": MAX_PLAN_RETRY}
    assert route_after_validator(state) == END


def test_missing_counters_default_to_zero():
    """计数未写入时按 0 处理，不能因此崩溃或误判为超上限。"""
    assert route_after_validator({"failure_type": "insufficient"}) == RETRIEVER_NODE
    assert route_after_validator({"failure_type": "conflict"}) == PLANNER_NODE


def test_unknown_or_missing_failure_terminates():
    assert route_after_validator({"failure_type": "none"}) == END
    assert route_after_validator({"failure_type": "unknown"}) == END
    assert route_after_validator({"question": "q"}) == END


def test_decide_route_exposes_the_same_target_and_stable_reason():
    state = {"failure_type": "insufficient", "retrieval_retry_count": 0}
    decision = decide_route(state)

    assert decision.target == route_after_validator(state) == RETRIEVER_NODE
    assert decision.reason == "insufficient_retry"


def test_node_names_match_agent_ids():
    """路由返回的节点名必须是协议里真实存在的智能体 ID。"""
    agents = load_agent_configs()
    assert PLANNER_NODE in agents
    assert RETRIEVER_NODE in agents
