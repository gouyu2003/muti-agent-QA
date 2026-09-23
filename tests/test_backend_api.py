from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app import app, build_api_run_context
from backend.schemas import StreamEnvelope
from backend.streaming import encode_sse, stream_qa_events
from src.runtime.models import RunContext, TraceEvent
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository


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
    assert health.json() == {"status": "ok", "system_version": "v0"}
    assert system.status_code == 200
    assert len(system.json()["agents"]) == 3
    assert system.json()["dataset"]["name"] == "HotpotQA"


def test_question_validation_rejects_blank_question(api_client):
    client, _repository = api_client
    response = client.post("/api/v1/qa/stream", json={"question": "   "})
    assert response.status_code == 422


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


def test_api_context_snapshots_current_v0_configuration():
    context = build_api_run_context("Q", run_id="fixed-run-id")

    assert context.run_id == "fixed-run-id"
    assert context.entrypoint == "api"
    assert context.method_id == "multi_agent_adaptive_v0"
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
