"""运行时公共数据契约。

本包定义运行记录数据契约与 SQLite 持久化；执行流程将在 RunService 中接入。
"""

from src.runtime.models import (
    RunContext,
    RunDetail,
    RunError,
    RunMetrics,
    RunRecord,
    RunStatsSummary,
    TraceEvent,
)
from src.runtime.repository import (
    InvalidRunTransitionError,
    RepositoryError,
    RunAlreadyExistsError,
    RunNotFoundError,
    TraceSequenceConflictError,
)
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.service import RunService

__all__ = [
    "RunContext",
    "RunDetail",
    "RunError",
    "RunMetrics",
    "RunRecord",
    "RunStatsSummary",
    "RunService",
    "SQLiteRunRepository",
    "TraceEvent",
    "InvalidRunTransitionError",
    "RepositoryError",
    "RunAlreadyExistsError",
    "RunNotFoundError",
    "TraceSequenceConflictError",
]
