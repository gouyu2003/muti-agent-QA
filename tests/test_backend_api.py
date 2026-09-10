from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.app import app
from backend.schemas import StreamEnvelope
from backend.streaming import encode_sse, stream_qa_events


class FakeGraph:
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


def _event_names(chunks: list[str]) -> list[str]:
    return [chunk.splitlines()[0].removeprefix("event: ") for chunk in chunks]


def test_health_and_system_info():
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        system = client.get("/api/v1/system")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "system_version": "v0"}
    assert system.status_code == 200
    assert len(system.json()["agents"]) == 3
    assert system.json()["dataset"]["name"] == "HotpotQA"


def test_question_validation_rejects_blank_question():
    with TestClient(app) as client:
        response = client.post("/api/v1/qa/stream", json={"question": "   "})
    assert response.status_code == 422


def test_stream_emits_stable_event_sequence_and_final_result():
    chunks = list(stream_qa_events("Q", graph_factory=FakeGraph))
    assert _event_names(chunks) == [
        "run_started",
        "node_started",
        "node_completed",
        "node_started",
        "node_completed",
        "run_completed",
    ]

    completed_data = json.loads(chunks[-1].split("data: ", 1)[1])
    assert completed_data["payload"]["result"]["answer"] == "A"
    assert completed_data["payload"]["result"]["verified"] is True


def test_encode_sse_preserves_chinese_payload():
    chunk = encode_sse(
        "run_failed",
        StreamEnvelope(run_id="r1", timestamp="now", payload={"message": "配置缺失"}),
    )
    assert "配置缺失" in chunk
    assert chunk.endswith("\n\n")


def test_stream_converts_internal_exception_to_public_error():
    def broken_graph():
        raise ValueError("private detail")

    chunks = list(stream_qa_events("Q", graph_factory=broken_graph))
    assert _event_names(chunks) == ["run_started", "run_failed"]
    assert "private detail" not in chunks[-1]
