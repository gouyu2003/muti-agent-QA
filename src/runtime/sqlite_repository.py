"""RunRepository 的 SQLite 实现。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from src.runtime.models import (
    RunContext,
    RunDetail,
    RunEntryPoint,
    RunError,
    RunMetrics,
    RunRecord,
    RunStatsSummary,
    RunStatus,
    TraceEvent,
)
from src.runtime.repository import (
    InvalidRunTransitionError,
    RepositoryError,
    RunAlreadyExistsError,
    RunNotFoundError,
    TraceSequenceConflictError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "runtime" / "runs.sqlite3"
SCHEMA_VERSION = 1


_CREATE_SCHEMA_META_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    applied_at TEXT NOT NULL
);
"""


_CREATE_V1_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    entrypoint TEXT NOT NULL
        CHECK (entrypoint IN ('api', 'cli', 'batch')),
    question TEXT NOT NULL,

    source_id TEXT,
    record_id TEXT,
    workflow_id TEXT NOT NULL,
    method_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    protocol_id TEXT NOT NULL,
    code_version TEXT NOT NULL,
    config_snapshot_json TEXT NOT NULL,

    status TEXT NOT NULL
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    started_at TEXT NOT NULL,
    finished_at TEXT,

    final_state_json TEXT,
    error_type TEXT,
    error_message TEXT,

    duration_ms INTEGER
        CHECK (duration_ms IS NULL OR duration_ms >= 0),
    node_call_count INTEGER NOT NULL DEFAULT 0
        CHECK (node_call_count >= 0),
    llm_call_count INTEGER
        CHECK (llm_call_count IS NULL OR llm_call_count >= 0),
    prompt_tokens INTEGER
        CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
    completion_tokens INTEGER
        CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
    total_tokens INTEGER
        CHECK (total_tokens IS NULL OR total_tokens >= 0),
    estimated_cost_usd REAL
        CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
    plan_retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (plan_retry_count >= 0),
    retrieval_retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (retrieval_retry_count >= 0),
    validation_retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (validation_retry_count >= 0),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL
        CHECK (sequence >= 0),
    event_type TEXT NOT NULL
        CHECK (
            event_type IN (
                'run_started',
                'node_started',
                'node_completed',
                'route_decided',
                'run_completed',
                'run_failed',
                'run_cancelled'
            )
        ),
    timestamp TEXT NOT NULL,
    node TEXT,
    cycle INTEGER
        CHECK (cycle IS NULL OR cycle >= 1),
    duration_ms INTEGER
        CHECK (duration_ms IS NULL OR duration_ms >= 0),
    payload_json TEXT NOT NULL,

    FOREIGN KEY (run_id)
        REFERENCES runs(run_id)
        ON DELETE CASCADE,

    UNIQUE (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at
    ON runs(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_runs_status
    ON runs(status);

CREATE INDEX IF NOT EXISTS idx_runs_entrypoint
    ON runs(entrypoint);

CREATE INDEX IF NOT EXISTS idx_runs_method_id
    ON runs(method_id);

CREATE INDEX IF NOT EXISTS idx_trace_events_run_sequence
    ON trace_events(run_id, sequence);
"""


def _default_db_path() -> Path:
    """读取数据库路径，未配置时使用项目内默认路径。"""

    configured = os.getenv("MAQA_RUN_DB_PATH")
    if not configured:
        return DEFAULT_DB_PATH

    path = Path(configured).expanduser()
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_to_text(value: datetime) -> str:
    """将带时区的时间统一转换成 UTC ISO 8601 字符串。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryError("运行时间必须包含时区信息")
    return value.astimezone(timezone.utc).isoformat()


def _datetime_from_text(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RepositoryError(f"数据库中存在无效时间：{value}") from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RepositoryError("数据库中的运行时间缺少时区信息")
    return parsed


def _dump_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise RepositoryError("运行数据无法序列化为 JSON") from exc


def _load_json(value: str | None, field_name: str) -> Any:
    if value is None:
        return None

    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RepositoryError(f"数据库字段 {field_name} 不是有效 JSON") from exc


class SQLiteRunRepository:
    """使用本地 SQLite 保存运行记录和轨迹事件。"""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = _default_db_path() if db_path is None else Path(db_path)

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """创建版本表、业务表与索引；重复调用不会删除已有数据。"""

        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.execute(_CREATE_SCHEMA_META_SQL)
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM schema_meta"
                ).fetchone()
                current_version = row["version"] if row else None

                if current_version is not None and current_version > SCHEMA_VERSION:
                    raise RepositoryError(
                        "数据库版本高于当前程序支持版本："
                        f"{current_version} > {SCHEMA_VERSION}"
                    )

                connection.executescript(_CREATE_V1_SCHEMA_SQL)
                if current_version is None:
                    connection.execute(
                        """
                        INSERT INTO schema_meta(version, applied_at)
                        VALUES (?, ?)
                        """,
                        (SCHEMA_VERSION, _datetime_to_text(_utc_now())),
                    )
        except RepositoryError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise RepositoryError("SQLite 数据库初始化失败") from exc

    def _row_to_run(self, row: sqlite3.Row) -> RunRecord:
        try:
            snapshot = _load_json(row["config_snapshot_json"], "config_snapshot_json")
            if not isinstance(snapshot, dict):
                raise RepositoryError('配置快照必须是 JSON 对象')
            metadata = snapshot.get("_run_metadata", {})
            if not isinstance(metadata, dict):
                raise RepositoryError('实验元数据必须是 JSON 对象')
            context = RunContext(
                run_id=row["run_id"],
                entrypoint=row["entrypoint"],
                question=row["question"],
                source_id=row["source_id"],
                record_id=row["record_id"],
                workflow_id=row["workflow_id"],
                method_id=row["method_id"],
                model_name=row["model_name"],
                protocol_id=row["protocol_id"],
                code_version=row["code_version"],
                config_snapshot=snapshot,
                experiment_id=metadata.get("experiment_id"),
                sample_id=metadata.get("sample_id"),
            )
            metrics = RunMetrics(
                duration_ms=row["duration_ms"],
                node_call_count=row["node_call_count"],
                llm_call_count=row["llm_call_count"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=row["total_tokens"],
                estimated_cost_usd=row["estimated_cost_usd"],
                plan_retry_count=row["plan_retry_count"],
                retrieval_retry_count=row["retrieval_retry_count"],
                validation_retry_count=row["validation_retry_count"],
            )
            error = None
            if row["error_type"] is not None:
                error = RunError(
                    error_type=row["error_type"],
                    message=row["error_message"],
                )

            return RunRecord(
                context=context,
                status=row["status"],
                started_at=_datetime_from_text(row["started_at"]),
                finished_at=(
                    _datetime_from_text(row["finished_at"])
                    if row["finished_at"] is not None
                    else None
                ),
                final_state=_load_json(row["final_state_json"], "final_state_json"),
                metrics=metrics,
                error=error,
            )
        except RepositoryError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise RepositoryError(
                f"运行记录 {row['run_id']} 无法恢复为 RunRecord"
            ) from exc

    def _row_to_event(self, row: sqlite3.Row) -> TraceEvent:
        try:
            return TraceEvent(
                run_id=row["run_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                timestamp=_datetime_from_text(row["timestamp"]),
                node=row["node"],
                cycle=row["cycle"],
                duration_ms=row["duration_ms"],
                payload=_load_json(row["payload_json"], "payload_json"),
            )
        except RepositoryError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise RepositoryError(
                "轨迹事件无法恢复："
                f"run_id={row['run_id']}, sequence={row['sequence']}"
            ) from exc

    def create_run(self, record: RunRecord) -> None:
        if record.status != "running":
            raise InvalidRunTransitionError("新建运行记录的状态必须是 running")

        now_text = _datetime_to_text(_utc_now())
        error_type = record.error.error_type if record.error else None
        error_message = record.error.message if record.error else None
        values = (
            record.context.run_id,
            record.context.entrypoint,
            record.context.question,
            record.context.source_id,
            record.context.record_id,
            record.context.workflow_id,
            record.context.method_id,
            record.context.model_name,
            record.context.protocol_id,
            record.context.code_version,
            _dump_json(record.context.config_snapshot),
            record.status,
            _datetime_to_text(record.started_at),
            (
                _datetime_to_text(record.finished_at)
                if record.finished_at is not None
                else None
            ),
            _dump_json(record.final_state) if record.final_state is not None else None,
            error_type,
            error_message,
            record.metrics.duration_ms,
            record.metrics.node_call_count,
            record.metrics.llm_call_count,
            record.metrics.prompt_tokens,
            record.metrics.completion_tokens,
            record.metrics.total_tokens,
            record.metrics.estimated_cost_usd,
            record.metrics.plan_retry_count,
            record.metrics.retrieval_retry_count,
            record.metrics.validation_retry_count,
            now_text,
            now_text,
        )

        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO runs (
                        run_id, entrypoint, question, source_id, record_id,
                        workflow_id, method_id, model_name, protocol_id,
                        code_version, config_snapshot_json, status, started_at,
                        finished_at, final_state_json, error_type, error_message,
                        duration_ms, node_call_count, llm_call_count,
                        prompt_tokens, completion_tokens, total_tokens,
                        estimated_cost_usd, plan_retry_count,
                        retrieval_retry_count, validation_retry_count,
                        created_at, updated_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    values,
                )
        except sqlite3.IntegrityError as exc:
            if "runs.run_id" in str(exc):
                raise RunAlreadyExistsError(
                    f"运行记录已经存在：{record.context.run_id}"
                ) from exc
            raise RepositoryError("创建运行记录失败") from exc
        except sqlite3.Error as exc:
            raise RepositoryError("创建运行记录失败") from exc

    def update_run(self, record: RunRecord) -> None:
        run_id = record.context.run_id
        error_type = record.error.error_type if record.error else None
        error_message = record.error.message if record.error else None

        try:
            with self._connection() as connection:
                existing_row = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if existing_row is None:
                    raise RunNotFoundError(f"运行记录不存在：{run_id}")

                existing = self._row_to_run(existing_row)
                if existing.context != record.context:
                    raise RepositoryError(f"运行上下文创建后不能修改：{run_id}")
                if existing.started_at != record.started_at:
                    raise RepositoryError(f"运行开始时间创建后不能修改：{run_id}")
                if existing.status != "running":
                    raise InvalidRunTransitionError(
                        f"终态运行记录不能再次更新：{run_id} ({existing.status})"
                    )

                cursor = connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, finished_at = ?, final_state_json = ?,
                        error_type = ?, error_message = ?, duration_ms = ?,
                        node_call_count = ?, llm_call_count = ?, prompt_tokens = ?,
                        completion_tokens = ?, total_tokens = ?,
                        estimated_cost_usd = ?, plan_retry_count = ?,
                        retrieval_retry_count = ?, validation_retry_count = ?,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        record.status,
                        (
                            _datetime_to_text(record.finished_at)
                            if record.finished_at is not None
                            else None
                        ),
                        (
                            _dump_json(record.final_state)
                            if record.final_state is not None
                            else None
                        ),
                        error_type,
                        error_message,
                        record.metrics.duration_ms,
                        record.metrics.node_call_count,
                        record.metrics.llm_call_count,
                        record.metrics.prompt_tokens,
                        record.metrics.completion_tokens,
                        record.metrics.total_tokens,
                        record.metrics.estimated_cost_usd,
                        record.metrics.plan_retry_count,
                        record.metrics.retrieval_retry_count,
                        record.metrics.validation_retry_count,
                        _datetime_to_text(_utc_now()),
                        run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RunNotFoundError(f"运行记录不存在：{run_id}")
        except RepositoryError:
            raise
        except sqlite3.Error as exc:
            raise RepositoryError(f"更新运行记录失败：{run_id}") from exc

    def get_run(self, run_id: str) -> RunRecord | None:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            return None if row is None else self._row_to_run(row)
        except RepositoryError:
            raise
        except sqlite3.Error as exc:
            raise RepositoryError(f"查询运行记录失败：{run_id}") from exc

    @staticmethod
    def _build_filters(
        *,
        status: RunStatus | None,
        entrypoint: RunEntryPoint | None,
        method_id: str | None,
        started_from: datetime | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if entrypoint is not None:
            clauses.append("entrypoint = ?")
            parameters.append(entrypoint)
        if method_id is not None:
            clauses.append("method_id = ?")
            parameters.append(method_id)
        if started_from is not None:
            clauses.append("julianday(started_at) >= julianday(?)")
            parameters.append(_datetime_to_text(started_from))
        return (
            (" WHERE " + " AND ".join(clauses)) if clauses else "",
            parameters,
        )

    def list_runs(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status: RunStatus | None = None,
        entrypoint: RunEntryPoint | None = None,
        method_id: str | None = None,
        started_from: datetime | None = None,
    ) -> list[RunRecord]:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        if offset < 0:
            raise ValueError("offset 不能小于 0")

        where_sql, parameters = self._build_filters(
            status=status, entrypoint=entrypoint, method_id=method_id,
            started_from=started_from,
        )
        sql = (
            "SELECT * FROM runs"
            + where_sql
            + " ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?"
        )
        parameters.extend([limit, offset])

        try:
            with self._connection() as connection:
                rows = connection.execute(sql, parameters).fetchall()
            return [self._row_to_run(row) for row in rows]
        except RepositoryError:
            raise
        except sqlite3.Error as exc:
            raise RepositoryError("查询运行列表失败") from exc

    def count_runs(
        self,
        *,
        status: RunStatus | None = None,
        entrypoint: RunEntryPoint | None = None,
        method_id: str | None = None,
        started_from: datetime | None = None,
    ) -> int:
        where_sql, parameters = self._build_filters(
            status=status, entrypoint=entrypoint, method_id=method_id,
            started_from=started_from,
        )
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM runs" + where_sql,
                    parameters,
                ).fetchone()
            return int(row["total"])
        except sqlite3.Error as exc:
            raise RepositoryError("统计运行记录失败") from exc

    def get_stats_summary(
        self,
        *,
        status: RunStatus | None = None,
        entrypoint: RunEntryPoint | None = None,
        method_id: str | None = None,
        started_from: datetime | None = None,
    ) -> RunStatsSummary:
        where_sql, parameters = self._build_filters(
            status=status, entrypoint=entrypoint, method_id=method_id,
            started_from=started_from,
        )
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT status, final_state_json, error_type, duration_ms, "
                    "plan_retry_count, retrieval_retry_count FROM runs" + where_sql,
                    parameters,
                ).fetchall()

            status_counts = {
                name: 0 for name in ("running", "completed", "failed", "cancelled")
            }
            failure_types: dict[str, int] = {}
            verified_runs = 0
            durations: list[int] = []
            retry_counts: list[int] = []
            for row in rows:
                run_status = row["status"]
                status_counts[run_status] += 1
                final_state = _load_json(row["final_state_json"], "final_state_json")
                if final_state is not None and not isinstance(final_state, dict):
                    raise RepositoryError("数据库字段 final_state_json 必须是对象")
                if (
                    run_status == "completed"
                    and final_state
                    and final_state.get("verified") is True
                ):
                    verified_runs += 1

                if run_status == "failed":
                    failure_type = row["error_type"]
                else:
                    failure_type = final_state.get("failure_type") if final_state else None
                if (
                    isinstance(failure_type, str)
                    and failure_type
                    and failure_type != "none"
                ):
                    failure_types[failure_type] = failure_types.get(failure_type, 0) + 1

                if row["duration_ms"] is not None:
                    durations.append(row["duration_ms"])
                if run_status != "running":
                    retry_counts.append(
                        row["plan_retry_count"] + row["retrieval_retry_count"]
                    )

            total = len(rows)
            completed = status_counts["completed"]
            return RunStatsSummary(
                total_runs=total,
                completed_runs=completed,
                failed_runs=status_counts["failed"],
                cancelled_runs=status_counts["cancelled"],
                running_runs=status_counts["running"],
                verified_runs=verified_runs,
                completion_rate=completed / total if total else 0.0,
                verification_pass_rate=verified_runs / completed if completed else 0.0,
                average_duration_ms=(
                    sum(durations) / len(durations) if durations else None
                ),
                average_retry_count=(
                    sum(retry_counts) / len(retry_counts) if retry_counts else None
                ),
                failure_types=dict(sorted(failure_types.items())),
            )
        except RepositoryError:
            raise
        except sqlite3.Error as exc:
            raise RepositoryError("汇总运行统计失败") from exc

    def append_event(self, event: TraceEvent) -> None:
        try:
            with self._connection() as connection:
                parent = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?", (event.run_id,)
                ).fetchone()
                if parent is None:
                    raise RunNotFoundError(f"运行记录不存在：{event.run_id}")
                if parent["status"] != "running":
                    raise InvalidRunTransitionError(
                        f"终态运行记录不能追加轨迹：{event.run_id}"
                    )

                sequence_row = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence
                    FROM trace_events
                    WHERE run_id = ?
                    """,
                    (event.run_id,),
                ).fetchone()
                expected_sequence = int(sequence_row["next_sequence"])
                if event.sequence != expected_sequence:
                    raise TraceSequenceConflictError(
                        "轨迹序号必须连续递增："
                        f"run_id={event.run_id}, expected={expected_sequence}, "
                        f"actual={event.sequence}"
                    )

                connection.execute(
                    """
                    INSERT INTO trace_events (
                        run_id, sequence, event_type, timestamp,
                        node, cycle, duration_ms, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.run_id,
                        event.sequence,
                        event.event_type,
                        _datetime_to_text(event.timestamp),
                        event.node,
                        event.cycle,
                        event.duration_ms,
                        _dump_json(event.payload),
                    ),
                )
        except (RunNotFoundError, InvalidRunTransitionError):
            raise
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise TraceSequenceConflictError(
                    "轨迹序号重复："
                    f"run_id={event.run_id}, sequence={event.sequence}"
                ) from exc
            raise RepositoryError("写入轨迹事件失败") from exc
        except RepositoryError:
            raise
        except sqlite3.Error as exc:
            raise RepositoryError("写入轨迹事件失败") from exc

    def get_events(self, run_id: str) -> list[TraceEvent]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM trace_events
                    WHERE run_id = ?
                    ORDER BY sequence ASC
                    """,
                    (run_id,),
                ).fetchall()
            return [self._row_to_event(row) for row in rows]
        except RepositoryError:
            raise
        except sqlite3.Error as exc:
            raise RepositoryError(f"查询运行轨迹失败：{run_id}") from exc

    def get_run_detail(self, run_id: str) -> RunDetail | None:
        run = self.get_run(run_id)
        if run is None:
            return None

        try:
            return RunDetail(run=run, events=self.get_events(run_id))
        except ValidationError as exc:
            raise RepositoryError(f"无法组装运行详情：{run_id}") from exc

    def export_runs_jsonl(
        self,
        *,
        status: RunStatus | None = None,
        entrypoint: RunEntryPoint | None = None,
        method_id: str | None = None,
        started_from: datetime | None = None,
    ) -> Iterator[str]:
        """导出每行一个 RunDetail 的 UTF-8 友好 JSON 文本。

        首版面向单机研究数据集，在同一个数据库快照中组装全部结果后关闭连接，
        再把内存中的行迭代给上层，避免流式响应期间长期占用 SQLite 连接。
        """

        where_sql, parameters = self._build_filters(
            status=status, entrypoint=entrypoint, method_id=method_id,
            started_from=started_from,
        )
        try:
            with self._connection() as connection:
                run_rows = connection.execute(
                    "SELECT * FROM runs"
                    + where_sql
                    + " ORDER BY started_at DESC, run_id DESC",
                    parameters,
                ).fetchall()

                lines: list[str] = []
                for run_row in run_rows:
                    run = self._row_to_run(run_row)
                    event_rows = connection.execute(
                        """
                        SELECT * FROM trace_events
                        WHERE run_id = ?
                        ORDER BY sequence ASC
                        """,
                        (run.context.run_id,),
                    ).fetchall()
                    detail = RunDetail(
                        run=run,
                        events=[self._row_to_event(row) for row in event_rows],
                    )
                    lines.append(
                        _dump_json(detail.model_dump(mode="json")) + "\n"
                    )
            return iter(lines)
        except RepositoryError:
            raise
        except (sqlite3.Error, ValidationError) as exc:
            raise RepositoryError("导出运行记录失败") from exc
