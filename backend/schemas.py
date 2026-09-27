"""FastAPI 对外暴露的稳定数据模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from src.runtime.models import RunRecord


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    workflow_id: str | None = Field(default=None, min_length=1)
    source_id: str | None = Field(default=None, min_length=1)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("问题不能为空")
        return normalized


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    system_version: str


class AgentInfo(BaseModel):
    agent_id: str
    name: str
    role: str
    description: str


class FlowEdge(BaseModel):
    source: str
    target: str
    condition: str


class DatasetInfo(BaseModel):
    name: str
    record_id: str
    question_type: str
    level: str
    context_count: int
    supporting_fact_count: int
    default_question: str


class SystemInfoResponse(BaseModel):
    name: str
    version: str
    protocol_id: str
    model_name: str
    agents: list[AgentInfo]
    flow: list[FlowEdge]
    retry_limits: dict[str, int]
    dataset: DatasetInfo


class WorkflowOption(BaseModel):
    workflow_id: str
    name: str
    description: str
    method_id: str
    entry_node: str
    agents: list[str]
    edges: list[dict[str, str]]
    retry_limits: dict[str, int]


class SourceOption(BaseModel):
    source_id: str
    name: str
    source_type: str
    is_demo: bool
    record_id: str
    question_type: str
    level: str
    context_count: int
    supporting_fact_count: int
    example_question: str


class SystemOptionsResponse(BaseModel):
    system_id: str
    system_version: str
    default_workflow_id: str
    default_source_id: str
    workflows: list[WorkflowOption]
    sources: list[SourceOption]


class StreamEnvelope(BaseModel):
    """约束 SSE data 的公共形状。"""

    run_id: str
    timestamp: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunListResponse(BaseModel):
    """分页历史列表；total 是筛选后、分页前的记录数。"""

    items: list[RunRecord]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
