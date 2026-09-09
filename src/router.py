"""Answer-Validator 之后的条件路由。

职责边界：
- 纯函数：不调用 LLM、不读取 .env、不修改 state、无随机性、无网络；
- 只回答"下一个节点是谁"，不负责执行、不负责计数；
- 计数器由节点写入（retrieval_retry_count 由 retriever_fn 递增，
  plan_retry_count 由 planner_fn 递增），本模块只读。

规则来源：system-design/protocols/multi_hop_qa.json
- edges: e3_pass / e3_insufficient / e3_conflict / e3_max_retry
- routing_rules.max_cycles: retrieval_retry_count 上限 3，plan_retry_count 上限 2
"""

from __future__ import annotations

from langgraph.graph import END

from src.state import AgentState

# 与协议 routing_rules.max_cycles 保持一致
MAX_RETRIEVAL_RETRY = 3
MAX_PLAN_RETRY = 2

PLANNER_NODE = "planner_v0"
RETRIEVER_NODE = "retriever_v0"


def route_after_validator(state: AgentState) -> str:
    """返回下一个节点名：END / "retriever_v0" / "planner_v0"。

    判断顺序与协议 edges 一致：先 pass，再 insufficient，再 conflict，
    最后兜底 END。任何未知或缺失状态都导向 END，保证图不会死循环。
    """
    # e3_pass：验证通过优先；即使 failure_type 残留旧值也不再重试
    if state.get("verified", False):
        return END

    failure_type = state.get("failure_type", "none")

    # e3_insufficient：证据不足，且未超过补充检索上限
    if failure_type == "insufficient":
        if int(state.get("retrieval_retry_count", 0)) < MAX_RETRIEVAL_RETRY:
            return RETRIEVER_NODE
        return END

    # e3_conflict：证据冲突，且未超过重规划上限
    if failure_type == "conflict":
        if int(state.get("plan_retry_count", 0)) < MAX_PLAN_RETRY:
            return PLANNER_NODE
        return END

    # failure_type 为 none（但未 verified）或未知取值：直接结束
    return END
