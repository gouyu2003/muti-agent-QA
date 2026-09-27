"""图测试：全部使用 fake 节点，离线、零 API 成本、不读取 .env。

覆盖四条路径：正常 / 证据不足补检索 / 证据冲突重规划 / 达到重试上限终止。
"""

from langgraph.checkpoint.memory import InMemorySaver

from src.graph import (
    ANSWER_VALIDATOR_NODE,
    PLANNER_NODE,
    RETRIEVER_NODE,
    build_graph,
    default_nodes,
)
from src.state import AgentState, EvidenceChunk, EvidenceGroup, SubQuestion
from src.runtime.specs import WorkflowSpec
from src.runtime.system import resolve_system

SQ: list[SubQuestion] = [
    {"id": "sq_1", "question": "q1", "keywords": ["k1"], "depends_on": []}
]
CHUNK: EvidenceChunk = {
    "text": "E1",
    "source_title": "T1",
    "source_paragraph": 0,
    "relevance_score": 1.0,
}
GROUP: EvidenceGroup = {
    "sq_id": "sq_1",
    "chunks": [CHUNK],
    "sufficiency": "sufficient",
    "insufficiency_note": "",
}

PASS = {
    "answer": "A",
    "verified": True,
    "failure_type": "none",
    "supporting_evidence": [{"sq_id": "sq_1", "text": "E1", "source_title": "T1"}],
    "verification_note": "ok",
}
INSUFFICIENT = {
    "answer": "",
    "verified": False,
    "failure_type": "insufficient",
    "insufficient_sq_ids": ["sq_1"],
    "retrieval_supplement_request": {
        "target_sq_id": "sq_1",
        "additional_keywords": ["k2"],
        "reason": "缺少关键证据",
    },
    "verification_note": "证据不足",
}
CONFLICT = {
    "answer": "",
    "verified": False,
    "failure_type": "conflict",
    "conflict_flag": True,
    "conflict_note": "两段证据矛盾",
    "verification_note": "证据冲突",
}


class FakeNodes:
    """按顺序回放 validator 的输出，并记录每个节点被调用的次数。"""

    def __init__(self, validator_results):
        self.validator_results = list(validator_results)
        self.calls = {PLANNER_NODE: 0, RETRIEVER_NODE: 0, ANSWER_VALIDATOR_NODE: 0}

    def planner(self, state: AgentState) -> dict:
        self.calls[PLANNER_NODE] += 1
        retry = int(state.get("plan_retry_count", 0)) + (
            1 if state.get("conflict_flag") else 0
        )
        return {
            "sub_questions": SQ,
            "retrieval_strategy": "sequential",
            "plan_retry_count": retry,
            "conflict_flag": False,
        }

    def retriever(self, state: AgentState) -> dict:
        self.calls[RETRIEVER_NODE] += 1
        retry = int(state.get("retrieval_retry_count", 0)) + (
            1 if state.get("retrieval_supplement_request") else 0
        )
        return {"evidence_chunks": [GROUP], "retrieval_retry_count": retry}

    def validator(self, state: AgentState) -> dict:
        index = self.calls[ANSWER_VALIDATOR_NODE]
        self.calls[ANSWER_VALIDATOR_NODE] += 1
        result = dict(self.validator_results[min(index, len(self.validator_results) - 1)])
        result["validation_retry_count"] = self.calls[ANSWER_VALIDATOR_NODE]
        return result

    def as_dict(self):
        return {
            PLANNER_NODE: self.planner,
            RETRIEVER_NODE: self.retriever,
            ANSWER_VALIDATOR_NODE: self.validator,
        }


def run_graph(fake: FakeNodes, thread_id: str = "thread-1") -> AgentState:
    """用 fake 节点跑完整图，返回最终状态。"""
    app = build_graph(nodes=fake.as_dict(), checkpointer=InMemorySaver())
    return app.invoke(
        {"question": "原始问题"},
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 30},
    )


def test_pass_path_visits_each_node_once():
    fake = FakeNodes([PASS])
    final = run_graph(fake)

    assert final["verified"] is True
    assert final["answer"] == "A"
    assert fake.calls == {PLANNER_NODE: 1, RETRIEVER_NODE: 1, ANSWER_VALIDATOR_NODE: 1}


def test_insufficient_triggers_supplementary_retrieval():
    fake = FakeNodes([INSUFFICIENT, PASS])
    final = run_graph(fake)

    assert final["verified"] is True
    assert fake.calls[RETRIEVER_NODE] == 2, "证据不足应回到 Retriever 补检索一次"
    assert fake.calls[ANSWER_VALIDATOR_NODE] == 2
    assert fake.calls[PLANNER_NODE] == 1, "补检索不应触发重规划"


def test_conflict_triggers_replan():
    fake = FakeNodes([CONFLICT, PASS])
    final = run_graph(fake)

    assert final["verified"] is True
    assert fake.calls[PLANNER_NODE] == 2, "证据冲突应回到 Planner 重规划一次"
    assert fake.calls[ANSWER_VALIDATOR_NODE] == 2
    assert final["plan_retry_count"] == 1


def test_max_retry_terminates_with_unverified_answer():
    fake = FakeNodes([INSUFFICIENT])
    final = run_graph(fake)

    assert final["verified"] is False
    assert final["failure_type"] == "insufficient"

    # 计数轨迹：0 →(补检索 1)→ 2 → 3，第 3 次后路由判定达上限并终止
    assert final["retrieval_retry_count"] == 3
    assert fake.calls[RETRIEVER_NODE] == 4
    assert fake.calls[ANSWER_VALIDATOR_NODE] == 4
    assert fake.calls[PLANNER_NODE] == 1, "证据不足不应触发重规划"


def test_real_nodes_are_wired_without_invoking_llm():
    """真实节点能成功建图（此处不 invoke，避免任何 API 调用）。"""
    app = build_graph(nodes=default_nodes())
    nodes = set(app.get_graph().nodes)

    assert {PLANNER_NODE, RETRIEVER_NODE, ANSWER_VALIDATOR_NODE} <= nodes


def test_separate_threads_do_not_share_state():
    """InMemorySaver 按 thread_id 隔离：两个会话互不影响。"""
    fake_a = FakeNodes([PASS])
    state_a = run_graph(fake_a, thread_id="a")

    fake_b = FakeNodes([INSUFFICIENT, PASS])
    state_b = run_graph(fake_b, thread_id="b")

    assert state_a["answer"] == "A"
    assert state_b["answer"] == "A"
    assert fake_a.calls[RETRIEVER_NODE] == 1
    assert fake_b.calls[RETRIEVER_NODE] == 2


def test_fixed_workflow_ends_after_one_validation_even_if_evidence_is_insufficient():
    fake = FakeNodes([INSUFFICIENT])
    workflow = resolve_system("multi_hop_qa_fixed").workflow
    graph = build_graph(nodes=fake.as_dict(), workflow=workflow)
    final = graph.invoke(
        {"question": "Q"}, config={"configurable": {"thread_id": "fixed"}}
    )
    assert final["verified"] is False
    assert fake.calls == {PLANNER_NODE: 1, RETRIEVER_NODE: 1, ANSWER_VALIDATOR_NODE: 1}


def test_feedback_retry_limit_comes_from_workflow_configuration():
    fake = FakeNodes([INSUFFICIENT])
    original = resolve_system().workflow
    raw = original.model_dump(by_alias=True)
    raw["max_cycles"]["retrieval_retry_count"] = 1
    workflow = WorkflowSpec.model_validate(raw)
    graph = build_graph(nodes=fake.as_dict(), workflow=workflow)
    final = graph.invoke(
        {"question": "Q"}, config={"configurable": {"thread_id": "one-retry"}}
    )
    assert final["retrieval_retry_count"] == 1
    assert fake.calls[RETRIEVER_NODE] == 2
    assert fake.calls[ANSWER_VALIDATOR_NODE] == 2
