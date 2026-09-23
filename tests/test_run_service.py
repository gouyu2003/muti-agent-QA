from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langgraph.graph import END

from src.runtime.models import RunContext
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository


BASE_TIME = datetime(2026, 9, 22, 5, 0, tzinfo=timezone.utc)


class StepNow:
    def __init__(self):
        self.calls = 0

    def __call__(self) -> datetime:
        value = BASE_TIME + timedelta(milliseconds=self.calls * 10)
        self.calls += 1
        return value


class StepClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.01
        return self.value


class FakeGraph:
    def __init__(self, parts: list[dict[str, Any]], error: Exception | None = None):
        self.parts = parts
        self.error = error
        self.input: dict[str, Any] | None = None
        self.config: dict[str, Any] | None = None
        self.stream_mode: list[str] | None = None
        self.version: str | None = None

    def stream(self, graph_input, *, config, stream_mode, version):
        self.input = graph_input
        self.config = config
        self.stream_mode = stream_mode
        self.version = version
        for part in self.parts:
            yield part
        if self.error is not None:
            raise self.error


def context(run_id: str, entrypoint: str = "api") -> RunContext:
    return RunContext(
        run_id=run_id,
        entrypoint=entrypoint,
        question="Who held the government position?",
        source_id="hotpotqa_fixture",
        record_id="fixture_001",
        workflow_id="multi_hop_qa_v0",
        method_id="multi_agent_adaptive_v0",
        model_name="deepseek-v3.1",
        protocol_id="multi_hop_qa_v0",
        code_version="test-version",
        config_snapshot={"retry_limits": {"retriever": 3, "planner": 2}},
    )


def repository(tmp_path: Path) -> SQLiteRunRepository:
    return SQLiteRunRepository(tmp_path / "runs.sqlite3")


def success_parts() -> list[dict[str, Any]]:
    return [
        {
            "type": "tasks",
            "data": {"name": "planner_v0", "input": {"question": "Q"}},
        },
        {
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
                    "private": "must not be persisted",
                }
            },
        },
        {
            "type": "tasks",
            "data": {"name": "retriever_v0", "input": {}},
        },
        {
            "type": "updates",
            "data": {
                "retriever_v0": {
                    "evidence_chunks": [],
                    "retrieval_retry_count": 0,
                }
            },
        },
        {
            "type": "tasks",
            "data": {"name": "answer_validator_v0", "input": {}},
        },
        {
            "type": "updates",
            "data": {
                "answer_validator_v0": {
                    "answer": "Chief of Protocol",
                    "verified": True,
                    "failure_type": "none",
                    "verification_note": "passed",
                    "supporting_evidence": [],
                    "validation_retry_count": 1,
                }
            },
        },
    ]


def make_service(
    tmp_path: Path, graph: FakeGraph
) -> tuple[RunService, SQLiteRunRepository]:
    repo = repository(tmp_path)
    service = RunService(
        repo,
        graph_factory=lambda _usage_collector: graph,
        now=StepNow(),
        clock=StepClock(),
    )
    return service, repo


def test_success_persists_the_same_ordered_events_and_completed_record(
    tmp_path: Path,
):
    graph = FakeGraph(success_parts())
    service, repo = make_service(tmp_path, graph)
    run_context = context("run-success")

    emitted = list(service.stream(run_context))
    detail = repo.get_run_detail(run_context.run_id)

    assert detail is not None
    assert emitted == detail.events
    assert [event.event_type for event in emitted] == [
        "run_started",
        "node_started",
        "node_completed",
        "node_started",
        "node_completed",
        "node_started",
        "node_completed",
        "route_decided",
        "run_completed",
    ]
    assert [event.sequence for event in emitted] == list(range(len(emitted)))
    assert emitted[-2].payload == {"target": END, "reason": "verified"}
    assert detail.run.status == "completed"
    assert detail.run.final_state is not None
    assert detail.run.final_state["answer"] == "Chief of Protocol"
    assert "private" not in detail.run.final_state
    assert detail.run.metrics.node_call_count == 3
    assert detail.run.metrics.validation_retry_count == 1
    assert detail.run.metrics.duration_ms is not None


def test_service_persists_llm_usage_collected_by_the_graph(tmp_path: Path):
    graph = FakeGraph(success_parts())

    def graph_factory(usage_collector):
        class UsageGraph:
            def stream(self, graph_input, *, config, stream_mode, version):
                responses = iter(
                    [
                        SimpleNamespace(
                            usage_metadata={
                                "input_tokens": 10,
                                "output_tokens": 4,
                                "total_tokens": 14,
                            }
                        ),
                        SimpleNamespace(
                            usage_metadata={
                                "input_tokens": 20,
                                "output_tokens": 6,
                                "total_tokens": 26,
                            }
                        ),
                    ]
                )
                chat = SimpleNamespace(invoke=lambda _prompt: next(responses))
                usage_collector.invoke(chat, "planner prompt")
                usage_collector.invoke(chat, "validator prompt")
                yield from graph.stream(
                    graph_input,
                    config=config,
                    stream_mode=stream_mode,
                    version=version,
                )

        return UsageGraph()

    repo = repository(tmp_path)
    service = RunService(
        repo,
        graph_factory=graph_factory,
        now=StepNow(),
        clock=StepClock(),
    )

    list(service.stream(context("run-usage")))
    record = repo.get_run("run-usage")

    assert record is not None
    assert record.metrics.llm_call_count == 2
    assert record.metrics.prompt_tokens == 30
    assert record.metrics.completion_tokens == 10
    assert record.metrics.total_tokens == 40
    assert record.metrics.estimated_cost_usd is None


def test_service_uses_run_id_as_thread_id_and_v2_task_update_stream(
    tmp_path: Path,
):
    graph = FakeGraph(success_parts())
    service, _repo = make_service(tmp_path, graph)

    list(service.stream(context("run-config")))

    assert graph.input == {"question": "Who held the government position?"}
    assert graph.config == {
        "configurable": {"thread_id": "run-config"},
        "recursion_limit": 30,
    }
    assert graph.stream_mode == ["tasks", "updates"]
    assert graph.version == "v2"


def test_route_event_reuses_router_decision_for_retry(tmp_path: Path):
    parts = [
        {
            "type": "tasks",
            "data": {"name": "answer_validator_v0", "input": {}},
        },
        {
            "type": "updates",
            "data": {
                "answer_validator_v0": {
                    "answer": "",
                    "verified": False,
                    "failure_type": "insufficient",
                    "insufficient_sq_ids": ["sq_1"],
                    "retrieval_retry_count": 0,
                    "validation_retry_count": 1,
                }
            },
        },
    ]
    service, repo = make_service(tmp_path, FakeGraph(parts))

    list(service.stream(context("run-route")))
    events = repo.get_events("run-route")
    route = next(event for event in events if event.event_type == "route_decided")

    assert route.payload == {
        "target": "retriever_v0",
        "reason": "insufficient_retry",
    }


def test_graph_failure_keeps_partial_trace_and_sanitizes_error(tmp_path: Path):
    graph = FakeGraph(
        success_parts()[:2],
        error=ValueError("private provider response and secret detail"),
    )
    service, repo = make_service(tmp_path, graph)

    emitted = list(service.stream(context("run-failed")))
    detail = repo.get_run_detail("run-failed")

    assert detail is not None
    assert emitted == detail.events
    assert emitted[-1].event_type == "run_failed"
    assert "private provider" not in emitted[-1].payload["message"]
    assert detail.run.status == "failed"
    assert detail.run.error is not None
    assert detail.run.error.error_type == "execution_error"
    assert detail.run.final_state is not None
    assert detail.run.final_state["sub_questions"][0]["id"] == "sq_1"


def test_model_configuration_failure_keeps_safe_actionable_message(tmp_path: Path):
    graph = FakeGraph([], error=RuntimeError("DEEPSEEK_API_KEY 未配置"))
    service, repo = make_service(tmp_path, graph)

    list(service.stream(context("run-model-config")))
    record = repo.get_run("run-model-config")

    assert record is not None
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.error_type == "model_configuration_error"
    assert record.error.message == "DEEPSEEK_API_KEY 未配置"


def test_closing_stream_marks_run_cancelled_and_preserves_partial_trace(
    tmp_path: Path,
):
    graph = FakeGraph(success_parts())
    service, repo = make_service(tmp_path, graph)
    events = service.stream(context("run-cancelled"))

    assert next(events).event_type == "run_started"
    assert next(events).event_type == "node_started"
    events.close()

    detail = repo.get_run_detail("run-cancelled")
    assert detail is not None
    assert detail.run.status == "cancelled"
    assert [event.event_type for event in detail.events] == [
        "run_started",
        "node_started",
        "run_cancelled",
    ]
    assert detail.events[-1].payload == {"reason": "consumer_disconnected"}


def test_run_consumes_stream_and_returns_persisted_detail(tmp_path: Path):
    service, _repo = make_service(tmp_path, FakeGraph(success_parts()))

    detail = service.run(context("run-cli", entrypoint="cli"))

    assert detail.run.status == "completed"
    assert detail.run.context.entrypoint == "cli"
    assert detail.events[-1].event_type == "run_completed"


def test_closing_after_completed_event_does_not_change_terminal_status(
    tmp_path: Path,
):
    service, repo = make_service(tmp_path, FakeGraph(success_parts()))
    events = service.stream(context("run-close-after-complete"))

    while next(events).event_type != "run_completed":
        pass
    events.close()

    detail = repo.get_run_detail("run-close-after-complete")
    assert detail is not None
    assert detail.run.status == "completed"
    assert detail.events[-1].event_type == "run_completed"
