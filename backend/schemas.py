"""FastAPI 对外暴露的稳定数据模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("问题不能为空")
        return normalized


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    system_version: str = "v0"


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


class StreamEnvelope(BaseModel):
    """约束 SSE data 的公共形状。"""

    run_id: str
    timestamp: str
    payload: dict[str, Any] = Field(default_factory=dict)
