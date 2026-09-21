"""运行时公共数据契约。

本包当前只定义运行记录、轨迹事件与指标模型；尚未接入持久化或执行流程。
"""

from src.runtime.models import (
    RunContext,
    RunDetail,
    RunError,
    RunMetrics,
    RunRecord,
    TraceEvent,
)

__all__ = [
    "RunContext",
    "RunDetail",
    "RunError",
    "RunMetrics",
    "RunRecord",
    "TraceEvent",
]
