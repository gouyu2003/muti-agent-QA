"""把 LangGraph 运行事件转换为前端稳定的 SSE 协议。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4

from backend.schemas import StreamEnvelope
from src.graph import build_graph

logger = logging.getLogger(__name__)

GraphFactory = Callable[[], Any]

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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_sse(event: str, envelope: StreamEnvelope) -> str:
    data = json.dumps(envelope.model_dump(), ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {data}\n\n"


def _envelope(run_id: str, payload: dict[str, Any]) -> StreamEnvelope:
    return StreamEnvelope(run_id=run_id, timestamp=utc_now(), payload=payload)


def sanitize_node_update(node: str, update: Any) -> dict[str, Any]:
    if not isinstance(update, dict):
        return {}
    allowed = NODE_FIELDS.get(node, ())
    return {field: update[field] for field in allowed if field in update}


def final_result(state: dict[str, Any]) -> dict[str, Any]:
    return {field: state[field] for field in FINAL_FIELDS if field in state}


def public_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if "DEEPSEEK_API_KEY" in message or "DEEPSEEK_BASE_URL" in message:
            return "model_configuration_error", message
    return "execution_error", "多智能体流程执行失败，请查看后端日志后重试。"


def stream_qa_events(
    question: str,
    graph_factory: GraphFactory = build_graph,
) -> Iterator[str]:
    run_id = str(uuid4())
    started_at = perf_counter()
    state: dict[str, Any] = {"question": question}
    node_cycles: dict[str, int] = {}

    yield encode_sse("run_started", _envelope(run_id, {"question": question}))

    try:
        graph = graph_factory()
        stream = graph.stream(
            {"question": question},
            config={
                "configurable": {"thread_id": run_id},
                "recursion_limit": 30,
            },
            stream_mode=["tasks", "updates"],
            version="v2",
        )

        for part in stream:
            part_type = part.get("type")
            data = part.get("data", {})

            if part_type == "tasks" and "result" not in data and "error" not in data:
                node = str(data.get("name", "unknown"))
                node_cycles[node] = node_cycles.get(node, 0) + 1
                yield encode_sse(
                    "node_started",
                    _envelope(run_id, {"node": node, "cycle": node_cycles[node]}),
                )
                continue

            if part_type != "updates" or not isinstance(data, dict):
                continue

            for node, raw_update in data.items():
                update = sanitize_node_update(node, raw_update)
                state.update(update)
                yield encode_sse(
                    "node_completed",
                    _envelope(
                        run_id,
                        {
                            "node": node,
                            "cycle": node_cycles.get(node, 1),
                            "output": update,
                        },
                    ),
                )

        elapsed_ms = round((perf_counter() - started_at) * 1000)
        yield encode_sse(
            "run_completed",
            _envelope(
                run_id,
                {"duration_ms": elapsed_ms, "result": final_result(state)},
            ),
        )
    except GeneratorExit:
        logger.info("客户端中断运行 run_id=%s", run_id)
        raise
    except Exception as exc:
        logger.exception("多智能体运行失败 run_id=%s", run_id)
        code, message = public_error(exc)
        yield encode_sse(
            "run_failed",
            _envelope(run_id, {"code": code, "message": message}),
        )
