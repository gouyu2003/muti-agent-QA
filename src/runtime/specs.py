"""Validated configuration contracts for the supported stage-two runtime."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class StrictSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentSpec(StrictSpec):
    agent_id: str = Field(min_length=1)
    agent_type: Literal["planner", "retriever", "answer_validator", "expert", "lead"]
    config: dict[str, Any]


class KnowledgeSourceSpec(StrictSpec):
    source_id: str = Field(min_length=1)
    source_type: Literal["hotpotqa_record", "hotpotqa_context", "document_corpus"]
    path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    is_demo: bool = False


class RouteSpec(StrictSpec):
    edge_id: str = Field(min_length=1)
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    route_name: Literal[
        "always", "verified", "insufficient_retry", "conflict_retry", "fallback"
    ]
    condition: str | None = None
    description: str | None = None


class WorkflowSpec(StrictSpec):
    protocol_id: str = Field(min_length=1)
    method_id: str = Field(min_length=1)
    agents: list[str] = Field(min_length=1)
    entry_node: str
    edges: list[RouteSpec] = Field(min_length=1)
    max_cycles: dict[str, StrictInt] = Field(default_factory=dict)
    join_sources: list[str] = Field(default_factory=list)
    join_target: str | None = None
    start_nodes: list[str] = Field(default_factory=list)
    conditional_nodes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowSpec":
        nodes = set(self.agents)
        if len(nodes) != len(self.agents) or self.entry_node not in nodes:
            raise ValueError("workflow agents 必须唯一，entry_node 必须是其中一个节点")
        outgoing: dict[str, list[RouteSpec]] = {node: [] for node in nodes}
        if (not set(self.start_nodes + self.join_sources + self.conditional_nodes) <= nodes
                or (self.join_target is not None and self.join_target not in nodes)):
            raise ValueError('汇合、入口或条件节点不存在')
        for edge in self.edges:
            if edge.source not in nodes or edge.target not in nodes | {"END"}:
                raise ValueError(f"非法连线 {edge.edge_id}: {edge.source} -> {edge.target}")
            outgoing[edge.source].append(edge)
        for node, edges in outgoing.items():
            if not edges:
                raise ValueError(f"节点 {node} 缺少出口")
            names = [edge.route_name for edge in edges]
            if len(edges) == 1 and names[0] != "always":
                raise ValueError(f"节点 {node} 的单条出口必须使用 always")
            fanout = len(edges) > 1 and set(names) == {'always'} and bool(self.join_sources)
            if len(edges) > 1 and not fanout and ("always" in names or names.count("fallback") != 1):
                raise ValueError(f"节点 {node} 的条件路由需要唯一 fallback，不能混用 always")
            if len(edges) > 1 and not fanout and node not in ['answer_validator_v0', *self.conditional_nodes]:
                raise ValueError(f"阶段 2 仅支持 answer_validator_v0 的条件路由: {node}")
            if not fanout and len(set(names)) != len(names):
                raise ValueError(f"节点 {node} 的路由规则重复")
        for key, value in self.max_cycles.items():
            if key not in {"retrieval_retry_count", "plan_retry_count", "validation_retry_count"} or value < 0:
                raise ValueError(f"无效重试限制: {key}={value}")
        if any(edge.route_name == "insufficient_retry" for edge in self.edges):
            if "retrieval_retry_count" not in self.max_cycles:
                raise ValueError("缺少 retrieval_retry_count 上限")
        if any(edge.route_name == "conflict_retry" for edge in self.edges):
            if "plan_retry_count" not in self.max_cycles:
                raise ValueError("缺少 plan_retry_count 上限")
        reachable = {self.entry_node, *self.start_nodes}
        pending = list(reachable)
        while pending:
            current = pending.pop()
            for edge in outgoing.get(current, []):
                if edge.target not in reachable:
                    reachable.add(edge.target)
                    pending.append(edge.target)
        if not nodes <= reachable or "END" not in reachable:
            raise ValueError("协议存在不可达节点或无法到达 END")
        return self


class RuntimeRequest(StrictSpec):
    question: str = Field(min_length=1, max_length=2000)
    workflow_id: str | None = Field(default=None, min_length=1)
    source_id: str | None = Field(default=None, min_length=1)
    source_version: StrictInt | None = Field(default=None, ge=1)
    system_id: str | None = Field(default=None, min_length=1)
    system_version: str | None = Field(default=None, pattern=r'^v[1-9][0-9]*$')
    draft_revision: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def system_selection(self):
        if self.system_id:
            if (self.system_version is None) == (self.draft_revision is None):
                raise ValueError('指定系统时请选择发布版本或草稿修订号。')
            if any(v is not None for v in (self.workflow_id, self.source_id, self.source_version)):
                raise ValueError('系统版本运行不能覆盖流程或知识源。')
        elif self.system_version is not None or self.draft_revision is not None:
            raise ValueError('必须同时指定 system_id。')
        return self
