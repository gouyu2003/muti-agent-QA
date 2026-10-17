"""可观测运行的公共数据模型。

这些模型只描述稳定的数据契约，不负责数据库读写，也不触发 LangGraph 执行。
轨迹只保存可验证的节点状态、证据与路由决策，不保存模型私有思维链。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RunStatus = Literal["running", "completed", "failed", "cancelled"]
RunEntryPoint = Literal["api", "cli", "batch"]
TraceEventType = Literal[
    "run_started",
    "node_started",
    "node_completed",
    "route_decided",
    "run_completed",
    "run_failed",
    "run_cancelled",
]

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class RuntimeModel(BaseModel):
    """运行时模型的共同约束：拒绝未声明字段，避免日志结构静默漂移。"""

    model_config = ConfigDict(extra="forbid")


class RunContext(RuntimeModel):
    """一次运行开始前即可确定的实验上下文。"""

    run_id: str = Field(min_length=1)
    entrypoint: RunEntryPoint
    question: str = Field(min_length=1, max_length=2000)
    source_id: str | None = None
    record_id: str | None = None
    workflow_id: str = Field(min_length=1)
    method_id: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    protocol_id: str = Field(min_length=1)
    code_version: str = Field(min_length=1)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    experiment_id: str | None = None
    sample_id: str | None = None

    @model_validator(mode="after")
    def snapshot_experiment_metadata(self) -> "RunContext":
        if self.experiment_id is not None or self.sample_id is not None:
            self.config_snapshot = {**self.config_snapshot, "_run_metadata": {
                "experiment_id": self.experiment_id, "sample_id": self.sample_id}}
        return self


class RunError(RuntimeModel):
    """可持久化的公开错误；message 不应包含密钥或内部敏感信息。"""

    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1)


class RunMetrics(RuntimeModel):
    """单次运行的系统开销指标；暂时拿不到的用量字段保持为 None。"""

    duration_ms: int | None = Field(default=None, ge=0)
    node_call_count: int = Field(default=0, ge=0)
    llm_call_count: int | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    plan_retry_count: int = Field(default=0, ge=0)
    retrieval_retry_count: int = Field(default=0, ge=0)
    validation_retry_count: int = Field(default=0, ge=0)


class TraceEvent(RuntimeModel):
    """一次运行中按 sequence 严格排序的公开轨迹事件。"""

    run_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    event_type: TraceEventType
    timestamp: datetime
    node: str | None = None
    cycle: int | None = Field(default=None, ge=1)
    duration_ms: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class RunRecord(RuntimeModel):
    """一次运行的生命周期状态与最终公开结果。"""

    context: RunContext
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    final_state: dict[str, Any] | None = None
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    error: RunError | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "RunRecord":
        if self.status in TERMINAL_STATUSES and self.finished_at is None:
            raise ValueError("终态运行必须提供 finished_at")
        if self.status == "running" and self.finished_at is not None:
            raise ValueError("运行中记录不能提供 finished_at")
        if self.status == "failed" and self.error is None:
            raise ValueError("失败运行必须提供 error")
        if self.status != "failed" and self.error is not None:
            raise ValueError("只有失败运行可以提供 error")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at 不能早于 started_at")
        return self


class RunDetail(RuntimeModel):
    """供后续历史详情接口使用的聚合响应模型。"""

    run: RunRecord
    events: list[TraceEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_events(self) -> "RunDetail":
        run_id = self.run.context.run_id
        if any(event.run_id != run_id for event in self.events):
            raise ValueError("轨迹事件的 run_id 必须与运行记录一致")
        sequences = [event.sequence for event in self.events]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("轨迹事件 sequence 必须严格递增且不重复")
        return self


class RunStatsSummary(RuntimeModel):
    """运行统计。完成率以全部运行计，验证通过率以已完成运行为分母。"""

    total_runs: int = Field(ge=0)
    completed_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    cancelled_runs: int = Field(ge=0)
    running_runs: int = Field(ge=0)
    verified_runs: int = Field(ge=0)
    completion_rate: float = Field(
        ge=0, le=1, description="已完成运行数 / 全部运行数"
    )
    verification_pass_rate: float = Field(
        ge=0, le=1, description="verified=true 的已完成运行数 / 已完成运行数"
    )
    average_duration_ms: float | None = Field(
        default=None, ge=0, description="有耗时记录的运行的平均毫秒数"
    )
    average_retry_count: float | None = Field(
        default=None,
        ge=0,
        description="终态运行的平均重规划次数与补充检索次数之和",
    )
    failure_types: dict[str, int] = Field(default_factory=dict)
