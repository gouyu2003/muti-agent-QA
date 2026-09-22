"""运行记录 Repository 接口及领域异常。"""

from __future__ import annotations

from typing import Iterator, Protocol

from src.runtime.models import (
    RunDetail,
    RunEntryPoint,
    RunRecord,
    RunStatus,
    TraceEvent,
)


class RepositoryError(RuntimeError):
    """运行记录存储层基础异常。"""


class RunAlreadyExistsError(RepositoryError):
    """相同 run_id 的运行记录已经存在。"""


class RunNotFoundError(RepositoryError):
    """指定的运行记录不存在。"""


class TraceSequenceConflictError(RepositoryError):
    """同一次运行的轨迹序号不是下一个连续序号。"""


class InvalidRunTransitionError(RepositoryError):
    """运行记录发生了不允许的生命周期迁移。"""


class RunRepository(Protocol):
    """运行记录存储接口。

    上层只依赖这个接口，不依赖具体的 SQLite 实现。
    """

    def initialize(self) -> None:
        """初始化存储结构，重复调用必须安全。"""
        ...

    def create_run(self, record: RunRecord) -> None:
        """创建一条运行记录。"""
        ...

    def update_run(self, record: RunRecord) -> None:
        """更新已有运行的状态、结果和指标。"""
        ...

    def get_run(self, run_id: str) -> RunRecord | None:
        """根据 run_id 查询运行。"""
        ...

    def list_runs(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status: RunStatus | None = None,
        entrypoint: RunEntryPoint | None = None,
        method_id: str | None = None,
    ) -> list[RunRecord]:
        """分页查询运行记录。"""
        ...

    def count_runs(
        self,
        *,
        status: RunStatus | None = None,
        entrypoint: RunEntryPoint | None = None,
        method_id: str | None = None,
    ) -> int:
        """统计符合筛选条件的运行数量。"""
        ...

    def append_event(self, event: TraceEvent) -> None:
        """为运行追加一条轨迹事件。"""
        ...

    def get_events(self, run_id: str) -> list[TraceEvent]:
        """按照 sequence 顺序查询运行轨迹。"""
        ...

    def get_run_detail(self, run_id: str) -> RunDetail | None:
        """返回运行记录及其全部轨迹。"""
        ...

    def export_runs_jsonl(
        self,
        *,
        status: RunStatus | None = None,
        entrypoint: RunEntryPoint | None = None,
        method_id: str | None = None,
    ) -> Iterator[str]:
        """按运行开始时间倒序导出可重新解析的 RunDetail JSONL。"""
        ...
