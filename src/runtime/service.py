"""统一执行 LangGraph、产生公共轨迹并持久化运行记录。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from src.graph import ANSWER_VALIDATOR_NODE, build_graph
from src.router import decide_route
from src.runtime.models import (
    RunContext,
    RunDetail,
    RunError,
    RunMetrics,
    RunRecord,
    TraceEvent,
    TraceEventType,
)
from src.runtime.repository import RepositoryError, RunRepository


GraphFactory = Callable[[], Any]
NowFactory = Callable[[], datetime]
Clock = Callable[[], float]

RECURSION_LIMIT = 30

NODE_FIELDS: dict[str, tuple[str, ...]] = {
    "planner_v0": ("sub_questions", "retrieval_strategy", "plan_retry_count"),
    "retriever_v0": ("evidence_chunks", "retrieval_retry_count"),
    "answer_validator_v0": (
        "answer",
        "verified",
        "failure_type",
        "insufficient_sq_ids",
        "conflict_flag",
        "conflict_note",
        "conflict_pairs",
        "verification_note",
        "supporting_evidence",
        "validation_retry_count",
    ),
}

FINAL_FIELDS = (
    "question",
    "sub_questions",
    "retrieval_strategy",
    "plan_retry_count",
    "evidence_chunks",
    "retrieval_retry_count",
    "answer",
    "verified",
    "failure_type",
    "conflict_flag",
    "conflict_note",
    "verification_note",
    "supporting_evidence",
    "validation_retry_count",
)

PUBLIC_INPUT_FIELDS = FINAL_FIELDS + (
    "insufficient_sq_ids",
    "retrieval_supplement_request",
    "conflict_pairs",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _public_mapping(value: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {field: value[field] for field in allowed if field in value}


def _public_error(exc: Exception) -> RunError:
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if "DEEPSEEK_API_KEY" in message or "DEEPSEEK_BASE_URL" in message:
            return RunError(error_type="model_configuration_error", message=message)
    return RunError(
        error_type="execution_error",
        message="多智能体流程执行失败，请查看后端日志后重试。",
    )


def _counter(state: dict[str, Any], name: str) -> int:
    try:
        return max(0, int(state.get(name, 0)))
    except (TypeError, ValueError):
        return 0


class RunService:
    """API、CLI 和后续批量实验共同依赖的唯一执行服务。"""

    def __init__(
        self,
        repository: RunRepository,
        *,
        graph_factory: GraphFactory = build_graph,
        now: NowFactory = _utc_now,
        clock: Clock = perf_counter,
    ):
        self.repository = repository
        self.graph_factory = graph_factory
        self.now = now
        self.clock = clock
        self.repository.initialize()

    def _metrics(
        self,
        state: dict[str, Any],
        *,
        started_clock: float,
        node_call_count: int,
    ) -> RunMetrics:
        duration_ms = max(0, round((self.clock() - started_clock) * 1000))
        return RunMetrics(
            duration_ms=duration_ms,
            node_call_count=node_call_count,
            plan_retry_count=_counter(state, "plan_retry_count"),
            retrieval_retry_count=_counter(state, "retrieval_retry_count"),
            validation_retry_count=_counter(state, "validation_retry_count"),
        )

    def stream(self, context: RunContext) -> Iterator[TraceEvent]:
        """执行一次运行；每个返回事件在交给调用方前已经写入 Repository。"""

        started_at = self.now()
        started_clock = self.clock()
        state: dict[str, Any] = {"question": context.question}
        sequence = 0
        node_call_count = 0
        terminal_persisted = False
        node_cycles: dict[str, int] = {}
        node_started_clocks: dict[tuple[str, int], float] = {}

        self.repository.create_run(
            RunRecord(context=context, status="running", started_at=started_at)
        )

        def make_event(
            event_type: TraceEventType,
            *,
            payload: dict[str, Any] | None = None,
            node: str | None = None,
            cycle: int | None = None,
            duration_ms: int | None = None,
        ) -> TraceEvent:
            nonlocal sequence
            event = TraceEvent(
                run_id=context.run_id,
                sequence=sequence,
                event_type=event_type,
                timestamp=self.now(),
                node=node,
                cycle=cycle,
                duration_ms=duration_ms,
                payload=payload or {},
            )
            self.repository.append_event(event)
            sequence += 1
            return event

        yield make_event("run_started", payload={"question": context.question})

        try:
            graph = self.graph_factory()
            stream = graph.stream(
                {"question": context.question},
                config={
                    "configurable": {"thread_id": context.run_id},
                    "recursion_limit": RECURSION_LIMIT,
                },
                stream_mode=["tasks", "updates"],
                version="v2",
            )

            for part in stream:
                part_type = part.get("type")
                data = part.get("data", {})

                if part_type == "tasks" and "result" not in data and "error" not in data:
                    node = str(data.get("name", "unknown"))
                    cycle = node_cycles.get(node, 0) + 1
                    node_cycles[node] = cycle
                    node_call_count += 1
                    node_started_clocks[(node, cycle)] = self.clock()
                    public_input = _public_mapping(
                        data.get("input", state), PUBLIC_INPUT_FIELDS
                    )
                    yield make_event(
                        "node_started",
                        node=node,
                        cycle=cycle,
                        payload={"input": public_input},
                    )
                    continue

                if part_type != "updates" or not isinstance(data, dict):
                    continue

                for node, raw_update in data.items():
                    update = _public_mapping(raw_update, NODE_FIELDS.get(node, ()))
                    state.update(update)
                    cycle = node_cycles.get(node, 1)
                    node_started_clock = node_started_clocks.pop(
                        (node, cycle), started_clock
                    )
                    node_duration_ms = max(
                        0, round((self.clock() - node_started_clock) * 1000)
                    )
                    yield make_event(
                        "node_completed",
                        node=node,
                        cycle=cycle,
                        duration_ms=node_duration_ms,
                        payload={"output": update},
                    )

                    if node == ANSWER_VALIDATOR_NODE:
                        decision = decide_route(state)
                        yield make_event(
                            "route_decided",
                            node=node,
                            cycle=cycle,
                            payload={
                                "target": decision.target,
                                "reason": decision.reason,
                            },
                        )

            metrics = self._metrics(
                state,
                started_clock=started_clock,
                node_call_count=node_call_count,
            )
            final_state = _public_mapping(state, FINAL_FIELDS)
            completed = make_event(
                "run_completed",
                duration_ms=metrics.duration_ms,
                payload={"result": final_state},
            )
            self.repository.update_run(
                RunRecord(
                    context=context,
                    status="completed",
                    started_at=started_at,
                    finished_at=completed.timestamp,
                    final_state=final_state,
                    metrics=metrics,
                )
            )
            terminal_persisted = True
            yield completed
        except GeneratorExit:
            if terminal_persisted:
                raise
            metrics = self._metrics(
                state,
                started_clock=started_clock,
                node_call_count=node_call_count,
            )
            cancelled = make_event(
                "run_cancelled",
                duration_ms=metrics.duration_ms,
                payload={"reason": "consumer_disconnected"},
            )
            self.repository.update_run(
                RunRecord(
                    context=context,
                    status="cancelled",
                    started_at=started_at,
                    finished_at=cancelled.timestamp,
                    final_state=_public_mapping(state, FINAL_FIELDS),
                    metrics=metrics,
                )
            )
            raise
        except RepositoryError:
            raise
        except Exception as exc:
            metrics = self._metrics(
                state,
                started_clock=started_clock,
                node_call_count=node_call_count,
            )
            error = _public_error(exc)
            failed = make_event(
                "run_failed",
                duration_ms=metrics.duration_ms,
                payload={"code": error.error_type, "message": error.message},
            )
            self.repository.update_run(
                RunRecord(
                    context=context,
                    status="failed",
                    started_at=started_at,
                    finished_at=failed.timestamp,
                    final_state=_public_mapping(state, FINAL_FIELDS),
                    metrics=metrics,
                    error=error,
                )
            )
            yield failed

    def run(self, context: RunContext) -> RunDetail:
        """消费完整事件流并返回已持久化的运行详情，供 CLI/批处理复用。"""

        for _event in self.stream(context):
            pass
        detail = self.repository.get_run_detail(context.run_id)
        if detail is None:
            raise RepositoryError(f"运行完成后无法读取记录：{context.run_id}")
        return detail
