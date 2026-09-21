from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.runtime.models import (
    RunContext,
    RunDetail,
    RunError,
    RunMetrics,
    RunRecord,
    TraceEvent,
)


NOW = datetime(2026, 9, 21, 13, 55, tzinfo=timezone.utc)


def context(run_id: str = "run-1") -> RunContext:
    return RunContext(
        run_id=run_id,
        entrypoint="api",
        question="Who held the government position?",
        source_id="hotpotqa_fixture",
        record_id="fixture_001",
        workflow_id="multi_hop_qa_v0",
        method_id="multi_agent_adaptive_v0",
        model_name="deepseek-v3.1",
        protocol_id="multi_hop_qa_v0",
        code_version="2ebad0d",
        config_snapshot={"retry_limits": {"retriever": 3}},
    )


def test_running_record_has_safe_metric_defaults():
    record = RunRecord(context=context(), status="running", started_at=NOW)

    assert record.metrics == RunMetrics()
    assert record.metrics.llm_call_count is None
    assert record.final_state is None


def test_completed_record_serializes_nested_context_and_result():
    record = RunRecord(
        context=context(),
        status="completed",
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        final_state={"answer": "Chief of Protocol", "verified": True},
        metrics=RunMetrics(duration_ms=1000, node_call_count=3),
    )

    payload = record.model_dump(mode="json")

    assert payload["context"]["method_id"] == "multi_agent_adaptive_v0"
    assert payload["final_state"]["verified"] is True
    assert payload["metrics"]["duration_ms"] == 1000


def test_terminal_record_requires_finished_at():
    with pytest.raises(ValidationError, match="finished_at"):
        RunRecord(context=context(), status="completed", started_at=NOW)


def test_failed_record_requires_public_error():
    with pytest.raises(ValidationError, match="error"):
        RunRecord(
            context=context(),
            status="failed",
            started_at=NOW,
            finished_at=NOW,
        )

    record = RunRecord(
        context=context(),
        status="failed",
        started_at=NOW,
        finished_at=NOW,
        error=RunError(error_type="runtime_error", message="运行失败"),
    )
    assert record.error.message == "运行失败"


def test_metrics_reject_negative_counts():
    with pytest.raises(ValidationError):
        RunMetrics(retrieval_retry_count=-1)


def test_run_detail_requires_matching_run_id_and_ordered_events():
    record = RunRecord(context=context(), status="running", started_at=NOW)
    started = TraceEvent(
        run_id="run-1",
        sequence=0,
        event_type="run_started",
        timestamp=NOW,
        payload={"question": "Q"},
    )
    node = TraceEvent(
        run_id="run-1",
        sequence=1,
        event_type="node_started",
        timestamp=NOW,
        node="planner_v0",
        cycle=1,
    )

    detail = RunDetail(run=record, events=[started, node])
    assert [event.sequence for event in detail.events] == [0, 1]

    with pytest.raises(ValidationError, match="run_id"):
        RunDetail(
            run=record,
            events=[started.model_copy(update={"run_id": "another-run"})],
        )

    with pytest.raises(ValidationError, match="sequence"):
        RunDetail(run=record, events=[node, started])


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        RunMetrics(duration_ms=1, unexpected=True)
