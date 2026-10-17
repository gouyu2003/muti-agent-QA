from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app import app, build_api_run_context
from backend.app import system_info
from backend.schemas import StreamEnvelope
from backend.streaming import encode_sse, stream_qa_events
from src.runtime.models import RunContext, RunError, RunMetrics, RunRecord, TraceEvent
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.system import resolve_system


class FakeGraph:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def stream(self, *_args, **_kwargs):
        yield {
            "type": "tasks",
            "data": {"name": "planner_v0", "input": {"question": "Q"}},
        }
        yield {
            "type": "updates",
            "data": {
                "planner_v0": {
                    "sub_questions": [
                        {
                            "id": "sq_1",
                            "question": "Q1",
                            "keywords": [],
                            "depends_on": [],
                        }
                    ],
                    "retrieval_strategy": "sequential",
                    "plan_retry_count": 0,
                }
            },
        }
        yield {
            "type": "tasks",
            "data": {"name": "answer_validator_v0", "input": {}},
        }
        yield {
            "type": "updates",
            "data": {
                "answer_validator_v0": {
                    "answer": "A",
                    "verified": True,
                    "failure_type": "none",
                    "verification_note": "通过",
                    "supporting_evidence": [],
                }
            },
        }
        if self.error is not None:
            raise self.error


def _context(run_id: str = "run-api-test") -> RunContext:
    return RunContext(
        run_id=run_id,
        entrypoint="api",
        question="Q",
        source_id="hotpotqa_fixture",
        record_id="fixture_001",
        workflow_id="multi_hop_qa_v0",
        method_id="multi_agent_adaptive_v0",
        model_name="test-model",
        protocol_id="multi_hop_qa_v0",
        code_version="test-version",
        config_snapshot={},
    )


def _service(
    tmp_path: Path, graph: FakeGraph | None = None
) -> tuple[RunService, SQLiteRunRepository]:
    repository = SQLiteRunRepository(tmp_path / "runs.sqlite3")
    service = RunService(
        repository,
        graph_factory=lambda _usage_collector: graph or FakeGraph(),
    )
    return service, repository


@pytest.fixture
def api_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, SQLiteRunRepository]]:
    service, repository = _service(tmp_path)
    original_factory = app.state.run_service_factory
    app.state.run_service_factory = lambda: service
    try:
        with TestClient(app) as client:
            yield client, repository
    finally:
        app.state.run_service_factory = original_factory


def _event_names(chunks: list[str]) -> list[str]:
    return [chunk.splitlines()[0].removeprefix("event: ") for chunk in chunks]


def _parse_sse_text(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.replace("\r\n", "\n").strip().split("\n\n"):
        lines = block.splitlines()
        event_type = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event_type, data))
    return events


def test_health_and_system_info(api_client):
    client, _repository = api_client
    health = client.get("/api/v1/health")
    system = client.get("/api/v1/system")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok", "system_version": resolve_system().system_version
    }
    assert system.status_code == 200
    assert len(system.json()["agents"]) == 3
    assert system.json()["dataset"]["name"] == "HotpotQA"


def test_system_options_are_validated_and_expose_only_display_metadata(api_client):
    client, _repository = api_client
    response = client.get("/api/v1/system/options")
    assert response.status_code == 200
    body = response.json()
    assert body["default_workflow_id"] == "multi_hop_qa_v0"
    assert body["default_source_id"] == "hotpotqa_fixture"
    assert {item["workflow_id"] for item in body["workflows"]} == {
        "multi_hop_qa_v0", "multi_hop_qa_fixed"
    }
    fixed = next(item for item in body["workflows"] if item["workflow_id"] == "multi_hop_qa_fixed")
    assert fixed["retry_limits"] == {}
    assert not any(edge["route_name"].endswith("retry") for edge in fixed["edges"])
    demo = next(item for item in body["sources"] if item["source_id"] == "demo_fixture_002")
    assert demo["is_demo"] is True
    assert demo["example_question"]
    assert "path" not in str(body)
    assert "resolved_path" not in str(body)


def test_system_info_accepts_a_fixed_workflow_without_condition_text(monkeypatch):
    monkeypatch.setattr(
        "backend.app.resolve_system",
        lambda **_kwargs: resolve_system("multi_hop_qa_fixed"),
    )
    info = system_info()
    assert info.protocol_id == "multi_hop_qa_fixed"
    assert info.flow[-1].condition == "always"


def test_question_validation_rejects_blank_question(api_client):
    client, _repository = api_client
    response = client.post("/api/v1/qa/stream", json={"question": "   "})
    assert response.status_code == 422


def test_api_selects_workflow_and_source_before_streaming(api_client):
    client, repository = api_client
    response = client.post(
        "/api/v1/qa/stream",
        json={"question": "Which city?", "workflow_id": "multi_hop_qa_fixed",
              "source_id": "demo_fixture_002"},
    )
    assert response.status_code == 200
    events = _parse_sse_text(response.text)
    run_id = events[0][1]["run_id"]
    detail = repository.get_run_detail(run_id)
    assert detail is not None
    assert detail.run.context.workflow_id == "multi_hop_qa_fixed"
    assert detail.run.context.source_id == "demo_fixture_002"
    assert detail.run.context.config_snapshot["source"]["sha256"]
    route = next(event for event in detail.events if event.event_type == "route_decided")
    assert route.payload == {"target": "__end__", "reason": "always"}


def test_api_rejects_unknown_workflow_before_creating_run(api_client):
    client, repository = api_client
    response = client.post(
        "/api/v1/qa/stream", json={"question": "Q", "workflow_id": "unknown"}
    )
    assert response.status_code == 422
    assert repository.count_runs() == 0


def test_stream_emits_compatible_sse_and_persists_the_same_run(tmp_path: Path):
    service, repository = _service(tmp_path)
    context = _context()

    chunks = list(stream_qa_events(context, service))

    assert _event_names(chunks) == [
        "run_started",
        "node_started",
        "node_completed",
        "node_started",
        "node_completed",
        "route_decided",
        "run_completed",
    ]
    started_data = json.loads(chunks[1].split("data: ", 1)[1])
    assert started_data["payload"]["node"] == "planner_v0"
    assert started_data["payload"]["cycle"] == 1

    completed_data = json.loads(chunks[-1].split("data: ", 1)[1])
    assert completed_data["payload"]["result"]["answer"] == "A"
    assert completed_data["payload"]["result"]["verified"] is True
    assert completed_data["payload"]["duration_ms"] >= 0

    detail = repository.get_run_detail(context.run_id)
    assert detail is not None
    assert detail.run.status == "completed"
    assert [event.event_type for event in detail.events] == _event_names(chunks)


def test_fastapi_endpoint_uses_run_service_and_persists_run(api_client):
    client, repository = api_client

    response = client.post("/api/v1/qa/stream", json={"question": "Q"})

    assert response.status_code == 200
    events = _parse_sse_text(response.text)
    assert events[0][0] == "run_started"
    assert events[-1][0] == "run_completed"

    run_id = events[0][1]["run_id"]
    assert all(data["run_id"] == run_id for _event, data in events)
    detail = repository.get_run_detail(run_id)
    assert detail is not None
    assert detail.run.status == "completed"
    assert detail.run.context.entrypoint == "api"
    assert detail.run.context.question == "Q"
    assert detail.run.context.source_id == "hotpotqa_fixture"
    assert detail.run.context.record_id is not None


def test_api_context_snapshots_current_v1_configuration_with_compatible_workflow_id():
    context = build_api_run_context("Q", run_id="fixed-run-id")

    assert context.run_id == "fixed-run-id"
    assert context.entrypoint == "api"
    assert context.method_id == "multi_agent_adaptive_v1"
    assert context.workflow_id == "multi_hop_qa_v0"
    assert context.protocol_id == "multi_hop_qa_v0"
    assert context.source_id == "hotpotqa_fixture"
    assert context.record_id
    assert context.config_snapshot["protocol"]["protocol_id"] == "multi_hop_qa_v0"


def test_encode_sse_preserves_chinese_payload():
    chunk = encode_sse(
        "run_failed",
        StreamEnvelope(run_id="r1", timestamp="now", payload={"message": "配置缺失"}),
    )
    assert "配置缺失" in chunk
    assert chunk.endswith("\n\n")


def test_stream_converts_infrastructure_exception_to_public_error():
    class BrokenService:
        def stream(self, _context):
            raise ValueError("private database detail")

    chunks = list(stream_qa_events(_context("run-broken"), BrokenService()))

    assert _event_names(chunks) == ["run_failed"]
    assert "private database detail" not in chunks[0]
    assert "streaming_error" in chunks[0]


def test_trace_event_adapter_preserves_timestamp_and_public_fields():
    event = TraceEvent(
        run_id="r1",
        sequence=2,
        event_type="node_completed",
        timestamp=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc),
        node="planner_v0",
        cycle=2,
        duration_ms=15,
        payload={"output": {"plan_retry_count": 1}},
    )

    class OneEventService:
        def stream(self, _context):
            yield event

    chunk = list(stream_qa_events(_context("r1"), OneEventService()))[0]
    data = json.loads(chunk.split("data: ", 1)[1])

    assert data["timestamp"] == "2026-09-22T08:00:00+00:00"
    assert data["payload"] == {
        "output": {"plan_retry_count": 1},
        "node": "planner_v0",
        "cycle": 2,
        "duration_ms": 15,
    }


def _seed_history(repository: SQLiteRunRepository) -> None:
    started_at = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
    for index, (entrypoint, method_id, status, verified, failure_type) in enumerate(
        [
            ("api", "multi_agent_adaptive_v0", "completed", True, "none"),
            ("cli", "multi_agent_adaptive_v0", "completed", False, "insufficient"),
            ("api", "single_agent_rag", "failed", False, "execution_error"),
            ("cli", "single_agent_rag", "cancelled", False, "none"),
        ]
    ):
        run_id = f"history-{index}"
        context = _context(run_id).model_copy(
            update={"entrypoint": entrypoint, "method_id": method_id}
        )
        started = started_at + timedelta(minutes=index)
        repository.create_run(RunRecord(context=context, status="running", started_at=started))
        repository.append_event(
            TraceEvent(
                run_id=run_id,
                sequence=0,
                event_type="run_started",
                timestamp=started,
                payload={"说明": "测试记录"},
            )
        )
        repository.update_run(
            RunRecord(
                context=context,
                status=status,
                started_at=started,
                finished_at=started + timedelta(seconds=index + 1),
                final_state={
                    "answer": "答案" if verified else "",
                    "verified": verified,
                    "failure_type": failure_type if status != "failed" else "none",
                },
                metrics=RunMetrics(
                    duration_ms=(index + 1) * 1000,
                    plan_retry_count=index % 2,
                    retrieval_retry_count=index // 2,
                ),
                error=(
                    RunError(error_type="execution_error", message="公开错误")
                    if status == "failed"
                    else None
                ),
            )
        )


def test_history_endpoints_return_ordered_paged_and_filtered_runs(api_client):
    client, repository = api_client
    _seed_history(repository)

    response = client.get("/api/v1/runs", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 4
    assert [item["context"]["run_id"] for item in response.json()["items"]] == [
        "history-2", "history-1"
    ]

    filtered = client.get(
        "/api/v1/runs",
        params={"status": "completed", "entrypoint": "cli", "method_id": "multi_agent_adaptive_v0"},
    )
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["context"]["run_id"] == "history-1"

    detail = client.get("/api/v1/runs/history-0")
    events = client.get("/api/v1/runs/history-0/events")
    assert detail.status_code == events.status_code == 200
    assert detail.json()["run"]["context"]["run_id"] == "history-0"
    assert detail.json()["events"] == events.json()
    assert events.json()[0]["sequence"] == 0
    assert client.get("/api/v1/runs/missing").status_code == 404
    assert client.get("/api/v1/runs/missing/events").status_code == 404


def test_summary_distinguishes_verification_from_completion_and_export_is_jsonl(api_client):
    client, repository = api_client
    _seed_history(repository)

    summary_response = client.get("/api/v1/stats/summary")
    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert (summary["total_runs"], summary["completed_runs"], summary["verified_runs"]) == (4, 2, 1)
    assert (summary["failed_runs"], summary["cancelled_runs"], summary["running_runs"]) == (1, 1, 0)
    assert summary["completion_rate"] == 0.5
    assert summary["verification_pass_rate"] == 0.5
    assert summary["average_duration_ms"] == 2500
    assert summary["average_retry_count"] == 1.0
    assert summary["failure_types"] == {"execution_error": 1, "insufficient": 1}

    filtered = client.get("/api/v1/stats/summary", params={"entrypoint": "cli"}).json()
    assert filtered["total_runs"] == 2
    assert filtered["verified_runs"] == 0

    exported = client.get("/api/v1/exports/runs.jsonl", params={"status": "completed"})
    assert exported.status_code == 200
    assert exported.headers["content-disposition"] == 'attachment; filename="runs.jsonl"'
    assert exported.headers["content-type"].startswith("application/x-ndjson")
    payloads = [json.loads(line) for line in exported.text.splitlines()]
    assert [item["run"]["context"]["run_id"] for item in payloads] == [
        "history-1", "history-0"
    ]
    assert payloads[1]["events"][0]["payload"]["说明"] == "测试记录"


def test_history_empty_state_and_invalid_filters(api_client):
    client, _repository = api_client
    assert client.get("/api/v1/runs").json()["items"] == []
    summary = client.get("/api/v1/stats/summary").json()
    assert summary["total_runs"] == 0
    assert summary["completion_rate"] == summary["verification_pass_rate"] == 0
    assert summary["average_duration_ms"] is None
    assert client.get("/api/v1/exports/runs.jsonl").text == ""
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "invalid"}):
        assert client.get("/api/v1/runs", params=params).status_code == 422


def test_time_filter_keeps_list_summary_and_export_in_sync(api_client):
    client, repository = api_client
    _seed_history(repository)
    since = {"started_from": "2026-09-23T08:02:00Z"}

    listing = client.get("/api/v1/runs", params=since).json()
    summary = client.get("/api/v1/stats/summary", params=since).json()
    exported = client.get("/api/v1/exports/runs.jsonl", params=since)

    assert listing["total"] == summary["total_runs"] == 2
    assert [item["context"]["run_id"] for item in listing["items"]] == [
        "history-3", "history-2"
    ]
    assert len(exported.text.splitlines()) == 2
    assert client.get(
        "/api/v1/runs", params={"started_from": "2026-09-23T08:02:00"}
    ).status_code == 422
