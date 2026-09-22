from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.runtime.models import (
    RunContext,
    RunError,
    RunMetrics,
    RunRecord,
    TraceEvent,
)
from src.runtime.repository import (
    InvalidRunTransitionError,
    RepositoryError,
    RunAlreadyExistsError,
    RunNotFoundError,
    TraceSequenceConflictError,
)
from src.runtime.sqlite_repository import SCHEMA_VERSION, SQLiteRunRepository


NOW = datetime(2026, 9, 22, 3, 30, tzinfo=timezone.utc)


def make_context(
    run_id: str,
    *,
    entrypoint: str = "api",
    method_id: str = "multi_agent_adaptive_v0",
) -> RunContext:
    return RunContext(
        run_id=run_id,
        entrypoint=entrypoint,
        question="谁担任了政府职位？",
        source_id="hotpotqa_fixture",
        record_id="fixture_001",
        workflow_id="multi_hop_qa_v0",
        method_id=method_id,
        model_name="deepseek-v3.1",
        protocol_id="multi_hop_qa_v0",
        code_version="14cd9e7",
        config_snapshot={"重试": {"retriever": 3}},
    )


def make_running(
    run_id: str,
    *,
    started_at: datetime = NOW,
    entrypoint: str = "api",
    method_id: str = "multi_agent_adaptive_v0",
) -> RunRecord:
    return RunRecord(
        context=make_context(
            run_id, entrypoint=entrypoint, method_id=method_id
        ),
        status="running",
        started_at=started_at,
    )


def make_completed(record: RunRecord) -> RunRecord:
    return RunRecord(
        context=record.context,
        status="completed",
        started_at=record.started_at,
        finished_at=record.started_at + timedelta(seconds=2),
        final_state={"answer": "Chief of Protocol", "verified": True},
        metrics=RunMetrics(
            duration_ms=2000,
            node_call_count=3,
            llm_call_count=2,
            total_tokens=120,
        ),
    )


def make_event(
    run_id: str,
    sequence: int,
    event_type: str = "node_started",
) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        timestamp=NOW + timedelta(milliseconds=sequence),
        node=None if event_type.startswith("run_") else "planner_v0",
        cycle=None if event_type.startswith("run_") else 1,
        payload={"说明": f"事件 {sequence}"},
    )


@pytest.fixture
def repo(tmp_path: Path) -> SQLiteRunRepository:
    repository = SQLiteRunRepository(tmp_path / "runtime" / "runs.sqlite3")
    repository.initialize()
    return repository


def test_initialize_is_idempotent_and_creates_expected_schema(tmp_path: Path):
    db_path = tmp_path / "nested" / "runs.sqlite3"
    repository = SQLiteRunRepository(db_path)

    repository.initialize()
    repository.initialize()

    assert db_path.exists()
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        versions = connection.execute(
            "SELECT version FROM schema_meta"
        ).fetchall()

    assert {"schema_meta", "runs", "trace_events"} <= tables
    assert {
        "idx_runs_started_at",
        "idx_runs_status",
        "idx_runs_entrypoint",
        "idx_runs_method_id",
        "idx_trace_events_run_sequence",
    } <= indexes
    assert versions == [(SCHEMA_VERSION,)]


def test_default_path_honors_absolute_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    configured_path = tmp_path / "configured" / "custom.sqlite3"
    monkeypatch.setenv("MAQA_RUN_DB_PATH", str(configured_path))

    repository = SQLiteRunRepository()
    repository.initialize()

    assert repository.db_path == configured_path
    assert configured_path.exists()


def test_initialize_rejects_newer_schema_version(tmp_path: Path):
    db_path = tmp_path / "future.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_meta (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, NOW.isoformat()),
        )

    with pytest.raises(RepositoryError, match="版本高于"):
        SQLiteRunRepository(db_path).initialize()


def test_create_and_read_round_trip_preserves_unicode_json(repo: SQLiteRunRepository):
    record = make_running("run-unicode")

    repo.create_run(record)

    restored = repo.get_run("run-unicode")
    assert restored == record
    assert restored is not None
    assert restored.context.config_snapshot["重试"]["retriever"] == 3


def test_create_requires_running_and_rejects_duplicate_id(repo: SQLiteRunRepository):
    running = make_running("run-create")
    repo.create_run(running)

    with pytest.raises(RunAlreadyExistsError):
        repo.create_run(running)

    with pytest.raises(InvalidRunTransitionError, match="running"):
        repo.create_run(make_completed(make_running("already-completed")))


def test_update_completes_run_and_preserves_context(repo: SQLiteRunRepository):
    running = make_running("run-complete")
    repo.create_run(running)

    completed = make_completed(running)
    repo.update_run(completed)

    assert repo.get_run("run-complete") == completed


def test_update_rejects_missing_run_and_immutable_fields(repo: SQLiteRunRepository):
    missing = make_running("missing")
    with pytest.raises(RunNotFoundError):
        repo.update_run(missing)

    running = make_running("run-immutable")
    repo.create_run(running)

    changed_context = running.model_copy(
        update={
            "context": running.context.model_copy(
                update={"question": "changed"}
            )
        }
    )
    with pytest.raises(RepositoryError, match="上下文"):
        repo.update_run(changed_context)

    changed_start = running.model_copy(
        update={"started_at": running.started_at + timedelta(seconds=1)}
    )
    with pytest.raises(RepositoryError, match="开始时间"):
        repo.update_run(changed_start)


def test_terminal_run_cannot_transition_again(repo: SQLiteRunRepository):
    running = make_running("run-terminal")
    completed = make_completed(running)
    repo.create_run(running)
    repo.update_run(completed)

    with pytest.raises(InvalidRunTransitionError, match="终态"):
        repo.update_run(completed)


def test_failed_run_round_trip_preserves_public_error(repo: SQLiteRunRepository):
    running = make_running("run-failed")
    failed = RunRecord(
        context=running.context,
        status="failed",
        started_at=running.started_at,
        finished_at=running.started_at + timedelta(seconds=1),
        error=RunError(error_type="execution_error", message="公开错误"),
    )
    repo.create_run(running)
    repo.update_run(failed)

    assert repo.get_run("run-failed") == failed


def test_cancelled_run_round_trip(repo: SQLiteRunRepository):
    running = make_running("run-cancelled")
    cancelled = RunRecord(
        context=running.context,
        status="cancelled",
        started_at=running.started_at,
        finished_at=running.started_at + timedelta(milliseconds=500),
        metrics=RunMetrics(duration_ms=500, node_call_count=1),
    )
    repo.create_run(running)
    repo.update_run(cancelled)

    assert repo.get_run("run-cancelled") == cancelled


def test_create_rejects_non_json_serializable_snapshot(repo: SQLiteRunRepository):
    running = make_running("run-bad-json")
    invalid = running.model_copy(
        update={
            "context": running.context.model_copy(
                update={"config_snapshot": {"bad": {1, 2, 3}}}
            )
        }
    )

    with pytest.raises(RepositoryError, match="JSON"):
        repo.create_run(invalid)


def test_list_and_count_support_pagination_order_and_filters(
    repo: SQLiteRunRepository,
):
    records = [
        make_running("run-1", started_at=NOW, entrypoint="api"),
        make_running(
            "run-2",
            started_at=NOW + timedelta(seconds=1),
            entrypoint="cli",
        ),
        make_running(
            "run-3",
            started_at=NOW + timedelta(seconds=2),
            entrypoint="api",
            method_id="single_agent_rag",
        ),
    ]
    for record in records:
        repo.create_run(record)
    repo.update_run(make_completed(records[0]))

    assert [
        item.context.run_id for item in repo.list_runs(limit=2)
    ] == ["run-3", "run-2"]
    assert [
        item.context.run_id for item in repo.list_runs(limit=2, offset=2)
    ] == ["run-1"]
    assert repo.count_runs() == 3
    assert repo.count_runs(status="completed") == 1
    assert repo.count_runs(entrypoint="api") == 2
    assert repo.count_runs(method_id="single_agent_rag") == 1
    assert [
        item.context.run_id
        for item in repo.list_runs(entrypoint="api", status="running")
    ] == ["run-3"]


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (101, 0), (20, -1)],
)
def test_list_rejects_invalid_pagination(
    repo: SQLiteRunRepository, limit: int, offset: int
):
    with pytest.raises(ValueError):
        repo.list_runs(limit=limit, offset=offset)


def test_events_require_parent_and_contiguous_sequence(repo: SQLiteRunRepository):
    with pytest.raises(RunNotFoundError):
        repo.append_event(make_event("missing", 0, "run_started"))

    repo.create_run(make_running("run-events"))
    repo.append_event(make_event("run-events", 0, "run_started"))

    with pytest.raises(TraceSequenceConflictError, match="expected=1"):
        repo.append_event(make_event("run-events", 2))

    repo.append_event(make_event("run-events", 1))
    with pytest.raises(TraceSequenceConflictError, match="expected=2"):
        repo.append_event(make_event("run-events", 1))

    assert [event.sequence for event in repo.get_events("run-events")] == [0, 1]


def test_terminal_run_rejects_additional_events(repo: SQLiteRunRepository):
    running = make_running("run-no-more-events")
    repo.create_run(running)
    repo.append_event(make_event(running.context.run_id, 0, "run_started"))
    repo.update_run(make_completed(running))

    with pytest.raises(InvalidRunTransitionError, match="终态"):
        repo.append_event(make_event(running.context.run_id, 1, "run_completed"))


def test_run_detail_and_restart_persistence(tmp_path: Path):
    db_path = tmp_path / "runs.sqlite3"
    first = SQLiteRunRepository(db_path)
    first.initialize()
    running = make_running("run-restart")
    first.create_run(running)
    first.append_event(make_event("run-restart", 0, "run_started"))
    first.append_event(make_event("run-restart", 1))

    reopened = SQLiteRunRepository(db_path)
    reopened.initialize()
    detail = reopened.get_run_detail("run-restart")

    assert detail is not None
    assert detail.run == running
    assert [event.sequence for event in detail.events] == [0, 1]
    assert reopened.get_run_detail("missing") is None
    assert reopened.get_events("missing") == []


def test_export_jsonl_is_parseable_ordered_filtered_and_unicode_safe(
    repo: SQLiteRunRepository,
):
    older = make_running("run-export-old", started_at=NOW)
    newer = make_running(
        "run-export-new",
        started_at=NOW + timedelta(seconds=1),
        method_id="single_agent_rag",
    )
    repo.create_run(older)
    repo.create_run(newer)
    repo.append_event(make_event("run-export-old", 0, "run_started"))

    all_lines = list(repo.export_runs_jsonl())
    payloads = [json.loads(line) for line in all_lines]

    assert all(line.endswith("\n") for line in all_lines)
    assert [item["run"]["context"]["run_id"] for item in payloads] == [
        "run-export-new",
        "run-export-old",
    ]
    assert payloads[1]["events"][0]["payload"]["说明"] == "事件 0"

    filtered = [
        json.loads(line)
        for line in repo.export_runs_jsonl(method_id="single_agent_rag")
    ]
    assert [item["run"]["context"]["run_id"] for item in filtered] == [
        "run-export-new"
    ]
