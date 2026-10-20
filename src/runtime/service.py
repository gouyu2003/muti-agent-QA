"""统一执行 LangGraph、产生公共轨迹并持久化运行记录。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from src.graph import ANSWER_VALIDATOR_NODE, build_graph, default_nodes
from src.llm_usage import LLMUsageCollector
from src.router import decide_route
from src.runtime.specs import WorkflowSpec
from src.runtime.system import retriever_from_snapshot
from src.state import merge_state
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


GraphFactory = Callable[[LLMUsageCollector], Any]
NowFactory = Callable[[], datetime]
Clock = Callable[[], float]

RECURSION_LIMIT = 30


def _build_default_graph(context: RunContext, usage_collector: LLMUsageCollector) -> Any:
    snapshot = context.config_snapshot
    if snapshot.get('schema_version') == '3':
        from src.runtime.protocol_runtime import build_protocol_graph
        return build_protocol_graph(context, usage_collector)
    workflow = WorkflowSpec.model_validate(snapshot["workflow_spec"])
    if snapshot.get('schema_version') == '2':
        from src.runtime.nodes import instance_nodes
        return build_graph(nodes=instance_nodes(snapshot, usage_collector), workflow=workflow)
    retriever = retriever_from_snapshot(snapshot)
    return build_graph(
        nodes=default_nodes(usage_collector, snapshot["agents"], retriever),
        workflow=workflow,
    )

NODE_FIELDS: dict[str, tuple[str, ...]] = {
    "planner_v0": ("sub_questions", "retrieval_strategy", "plan_retry_count",
                   "conflict_flag", "conflict_note", "conflict_pairs",
                   "retrieval_supplement_request", "insufficient_sq_ids", "model_diagnostics"),
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
        "retrieval_supplement_request",
        "model_diagnostics",
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
    "expert_results",
    "model_diagnostics",
    "protocol_calls",
    "termination_reason",
    "consensus_status",
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
    from src.runtime.protocol_runtime import ProtocolExecutionError
    if isinstance(exc, ProtocolExecutionError):
        return RunError(error_type='protocol_execution_error', message=str(exc))
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
        graph_factory: GraphFactory | None = None,
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
        usage_collector: LLMUsageCollector,
    ) -> RunMetrics:
        duration_ms = max(0, round((self.clock() - started_clock) * 1000))
        usage = usage_collector.snapshot()
        return RunMetrics(
            duration_ms=duration_ms,
            node_call_count=node_call_count,
            llm_call_count=usage.call_count,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            plan_retry_count=_counter(state, "plan_retry_count"),
            retrieval_retry_count=_counter(state, "retrieval_retry_count"),
            validation_retry_count=_counter(state, "validation_retry_count"),
            agent_usage=usage_collector.agent_usage(),
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
        usage_collector = LLMUsageCollector()
        stream = None
        processed_outputs = {}
        protocol_run = context.config_snapshot.get('schema_version') == '3'

        def output_fields(node):
            if protocol_run: return FINAL_FIELDS
            role = context.config_snapshot.get('agent_types', {}).get(node)
            if role == 'expert':
                return ('expert_results',) + (FINAL_FIELDS if context.workflow_id == 'single_expert' else ())
            return NODE_FIELDS.get(role + '_v0' if role else node, ()) + (
                ('sub_questions', 'evidence_chunks') if role == 'answer_validator' else ())

        def settle():
            usage_collector.cancelled.set()
            try:
                if stream is not None and hasattr(stream, 'close'):
                    stream.close()
            finally:
                usage_collector.wait_for_nodes()
            recovered = []
            for node, output in usage_collector.completed_outputs().items():
                public = _public_mapping(output, output_fields(node))
                if processed_outputs.get(node) != public:
                    merge_state(state, public)
                    call = next(iter(public.get('protocol_calls', {}).values()), {}) if protocol_run else {}
                    recovered.append(make_event('node_completed', node=call.get('agent_id', node), cycle=call.get('round', node_cycles.get(node, 1)),
                                                payload={**{k: call[k] for k in ('call_id', 'agent_id', 'stage_id', 'round', 'task_id', 'kind') if k in call},
                                                    'output': public, 'agent_name': call.get('agent_name', context.config_snapshot.get('agents', {}).get(node, {}).get('agent_name', node)),
                                                    'agent_type': call.get('agent_type', context.config_snapshot.get('agent_types', {}).get(node))}))
            return recovered

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
            workflow = (
                WorkflowSpec.model_validate(context.config_snapshot["workflow_spec"])
                if "workflow_spec" in context.config_snapshot and not protocol_run else None
            )
            graph = (
                self.graph_factory(usage_collector) if self.graph_factory is not None
                else _build_default_graph(context, usage_collector)
            )
            stream = graph.stream(
                {"question": context.question},
                config={
                    "configurable": {"thread_id": context.run_id},
                    "recursion_limit": sum(s['max_visits'] for s in context.config_snapshot['protocol_plan']['spec']['stages']) + 2 if protocol_run else RECURSION_LIMIT,
                },
                stream_mode=["custom"] if protocol_run else ["tasks", "updates"],
                version="v2",
            )

            for part in stream:
                part_type = part.get("type")
                data = part.get("data", {})

                if protocol_run and part_type == 'custom':
                    event_type = data['event_type']
                    payload = data.get('payload', {})
                    call_id = payload.get('call_id')
                    if event_type == 'node_started': node_call_count += 1
                    if event_type == 'node_completed':
                        update = _public_mapping(payload.get('output', {}), FINAL_FIELDS)
                        merge_state(state, update)
                        processed_outputs[call_id] = update
                    if event_type == 'route_decided':
                        merge_state(state, _public_mapping(payload.get('state_update', {}), FINAL_FIELDS))
                    yield make_event(event_type, node=data.get('node'), cycle=data.get('cycle'),
                                     duration_ms=data.get('duration_ms'), payload=payload)
                    continue

                if part_type == "tasks" and "result" not in data and "error" not in data:
                    node = str(data.get("name", "unknown"))
                    cycle = node_cycles.get(node, 0) + 1
                    node_cycles[node] = cycle
                    node_call_count += 1
                    node_started_clocks[(node, cycle)] = self.clock()
                    role = context.config_snapshot.get('agent_types', {}).get(node)
                    public_input = _public_mapping(data.get('input', state),
                        ('question', 'sub_questions') if role == 'expert' else PUBLIC_INPUT_FIELDS)
                    yield make_event(
                        "node_started",
                        node=node,
                        cycle=cycle,
                        payload={"input": public_input, 'agent_name': context.config_snapshot.get('agents', {}).get(node, {}).get('agent_name', node),
                                 'agent_type': context.config_snapshot.get('agent_types', {}).get(node)},
                    )
                    continue

                if part_type != "updates" or not isinstance(data, dict):
                    continue

                for node, raw_update in data.items():
                    update = _public_mapping(raw_update, output_fields(node))
                    merge_state(state, update)
                    processed_outputs[node] = update
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
                        payload={"output": update, 'agent_name': context.config_snapshot.get('agents', {}).get(node, {}).get('agent_name', node),
                                 'agent_type': context.config_snapshot.get('agent_types', {}).get(node)},
                    )

                    if (node == ANSWER_VALIDATOR_NODE or context.config_snapshot.get('agent_types', {}).get(node) == 'answer_validator'):
                        decision = decide_route(state, workflow, source=node)
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
                usage_collector=usage_collector,
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
            settle()
            metrics = self._metrics(
                state,
                started_clock=started_clock,
                node_call_count=node_call_count,
                usage_collector=usage_collector,
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
            usage_collector.cancelled.set()
            if stream is not None and hasattr(stream, 'close'):
                stream.close()
            usage_collector.wait_for_nodes()
            raise
        except Exception as exc:
            for recovered in settle():
                yield recovered
            metrics = self._metrics(
                state,
                started_clock=started_clock,
                node_call_count=node_call_count,
                usage_collector=usage_collector,
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
