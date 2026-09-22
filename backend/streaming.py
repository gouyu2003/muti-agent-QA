"""把 RunService 的公共轨迹事件转换为前端稳定的 SSE 协议。"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from backend.schemas import StreamEnvelope
from src.runtime.models import RunContext, TraceEvent
from src.runtime.service import RunService

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_sse(event: str, envelope: StreamEnvelope) -> str:
    data = json.dumps(envelope.model_dump(), ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {data}\n\n"


def trace_event_payload(event: TraceEvent) -> dict[str, Any]:
    """将轨迹字段合并到旧版 SSE payload，保持前端协议兼容。"""

    payload = dict(event.payload)
    if event.node is not None:
        payload["node"] = event.node
    if event.cycle is not None:
        payload["cycle"] = event.cycle
    if event.duration_ms is not None:
        payload["duration_ms"] = event.duration_ms
    return payload


def encode_trace_event(event: TraceEvent) -> str:
    envelope = StreamEnvelope(
        run_id=event.run_id,
        timestamp=event.timestamp.isoformat(),
        payload=trace_event_payload(event),
    )
    return encode_sse(event.event_type, envelope)


def stream_qa_events(
    context: RunContext,
    run_service: RunService,
) -> Iterator[str]:
    """消费 RunService 事件流，并逐条转换为 SSE 文本。"""

    events: Iterator[TraceEvent] | None = None
    try:
        events = run_service.stream(context)
        for event in events:
            yield encode_trace_event(event)
    except GeneratorExit:
        logger.info("客户端中断运行 run_id=%s", context.run_id)
        if events is not None and hasattr(events, "close"):
            events.close()
        raise
    except Exception:
        # RunService 会把图执行异常转换为已持久化的 run_failed 事件；这里仅兜底
        # Repository/编码器等基础设施异常，避免向客户端泄露内部细节。
        logger.exception("问答事件流基础设施失败 run_id=%s", context.run_id)
        yield encode_sse(
            "run_failed",
            StreamEnvelope(
                run_id=context.run_id,
                timestamp=utc_now(),
                payload={
                    "code": "streaming_error",
                    "message": "问答事件流失败，请查看后端日志后重试。",
                },
            ),
        )
