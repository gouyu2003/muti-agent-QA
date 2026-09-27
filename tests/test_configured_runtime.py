"""Behavioral checks for stage-two workflow and knowledge-source selection."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.runtime.context import build_v0_run_context
from src.runtime.specs import AgentSpec, WorkflowSpec
from src.runtime.system import DEFAULT_CATALOG_PATH, resolve_system, retriever_from_snapshot
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository


def test_two_sources_produce_distinct_evidence_and_traceable_source_ids():
    first = resolve_system()
    second = resolve_system(source_id="demo_fixture_002")
    first_chunks = retriever_from_snapshot(first.snapshot()).search("Corliss Archer")
    second_chunks = retriever_from_snapshot(second.snapshot()).search("Westhaven University")
    assert first_chunks and second_chunks
    assert {chunk["source_id"] for chunk in first_chunks} == {"hotpotqa_fixture"}
    assert {chunk["source_id"] for chunk in second_chunks} == {"demo_fixture_002"}
    assert first.record["_id"] != second.record["_id"]


def test_selected_workflow_and_source_are_saved_in_the_effective_snapshot():
    context = build_v0_run_context(
        "Which city?", entrypoint="cli",
        workflow_id="multi_hop_qa_fixed", source_id="demo_fixture_002",
    )
    assert context.workflow_id == "multi_hop_qa_fixed"
    assert context.source_id == "demo_fixture_002"
    assert context.record_id == "demo_fixture_002"
    assert context.config_snapshot["workflow_spec"]["edges"][-1]["route_name"] == "always"
    assert context.config_snapshot["source"]["sha256"]
    assert len(context.config_snapshot["config_sha256"]) == 64


def test_unknown_selection_and_illegal_edge_fail_before_execution():
    with pytest.raises(ValueError, match="未知 workflow_id"):
        resolve_system(workflow_id="missing")
    with pytest.raises(ValueError, match="source_id"):
        resolve_system(source_id="missing")
    raw = deepcopy(resolve_system().workflow.model_dump(by_alias=True))
    raw["edges"][0]["to"] = "missing_agent"
    with pytest.raises(ValueError, match="非法连线"):
        WorkflowSpec.model_validate(raw)
    raw = deepcopy(resolve_system().workflow.model_dump(by_alias=True))
    raw["max_cycles"]["retrieval_retry_count"] = -1
    with pytest.raises(ValueError, match="无效重试限制"):
        WorkflowSpec.model_validate(raw)
    raw = deepcopy(resolve_system().workflow.model_dump(by_alias=True))
    raw["edges"][0]["route_name"] = "python_expression"
    with pytest.raises(ValueError, match="route_name"):
        WorkflowSpec.model_validate(raw)
    with pytest.raises(ValueError, match="agent_type"):
        AgentSpec.model_validate(
            {"agent_id": "unsafe", "agent_type": "python_expression", "config": {}}
        )


def test_missing_selected_knowledge_file_fails_before_execution(tmp_path):
    catalog = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog["sources"][0]["path"] = "missing.json"
    path = tmp_path / "system.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ValueError, match="知识源文件不存在"):
        resolve_system(catalog_path=path)


def test_default_run_service_uses_selected_source_and_persists_its_evidence(
    tmp_path, monkeypatch,
):
    import src.graph as graph_module

    def planner(_state, **_kwargs):
        return {
            "sub_questions": [{"id": "sq_1", "question": "Westhaven University Bristol",
                               "keywords": [], "depends_on": []}],
            "retrieval_strategy": "sequential", "plan_retry_count": 0,
        }

    def validator(state, **_kwargs):
        chunk = state["evidence_chunks"][0]["chunks"][0]
        return {
            "answer": "Bristol", "verified": True, "failure_type": "none",
            "verification_note": "matched",
            "supporting_evidence": [{
                "sq_id": "sq_1", "text": chunk["text"],
                "source_title": chunk["source_title"], "source_id": chunk["source_id"],
            }],
        }

    monkeypatch.setattr(graph_module, "planner_fn", planner)
    monkeypatch.setattr(graph_module, "answer_validator_fn", validator)
    context = build_v0_run_context(
        "Where is Westhaven University?", entrypoint="cli",
        workflow_id="multi_hop_qa_fixed", source_id="demo_fixture_002",
    )
    detail = RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3")).run(context)
    assert detail.run.status == "completed"
    assert detail.run.final_state["answer"] == "Bristol"
    assert detail.run.final_state["evidence_chunks"][0]["chunks"][0]["source_id"] == "demo_fixture_002"
    assert detail.run.final_state["supporting_evidence"][0]["source_id"] == "demo_fixture_002"
    route = next(event for event in detail.events if event.event_type == "route_decided")
    assert route.payload == {"target": "__end__", "reason": "always"}
