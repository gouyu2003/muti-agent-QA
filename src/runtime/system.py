"""Resolve one system configuration before a run starts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.config_loader import REQUIRED_AGENT_FIELDS, load_json, validate_required_fields
from src.knowledge.bm25_retriever import BM25Retriever
from src.knowledge.hotpotqa_loader import load_fixture
from src.knowledge.corpus import load_corpus, corpus_retriever
from src.knowledge.store import KnowledgeStore, retriever_from_corpus_snapshot
from src.runtime.specs import AgentSpec, KnowledgeSourceSpec, WorkflowSpec

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "system-design" / "systems" / "default.json"
SOURCE_LOADERS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "hotpotqa_record": load_fixture,
    "document_corpus": load_corpus,
}
RETRIEVER_FACTORIES: dict[str, Callable[[dict[str, Any], str], BM25Retriever]] = {
    "hotpotqa_record": lambda record, source_id: BM25Retriever(record, source_id=source_id),
    "document_corpus": corpus_retriever,
}


@dataclass(frozen=True)
class ResolvedSystem:
    system_id: str
    system_version: str
    agents: dict[str, AgentSpec]
    workflow: WorkflowSpec
    protocol: dict[str, Any]
    source: KnowledgeSourceSpec
    source_path: Path
    source_digest: str
    record: dict[str, Any]
    catalog_path: Path
    source_metadata: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        agent_configs = {key: agent.config for key, agent in self.agents.items()}
        if self.source_metadata:
            agent_configs["retriever_v0"] = {
                **agent_configs["retriever_v0"],
                "knowledge_sources": [{"source_id": self.source.source_id, "source_type": "document_corpus",
                                       "source_version": self.source_metadata["version"]}],
            }
        effective = {
            "system_id": self.system_id,
            "system_version": self.system_version,
            "agents": agent_configs,
            "workflow_spec": self.workflow.model_dump(by_alias=True),
            "source_id": self.source.source_id,
            "source_sha256": self.source_digest,
            **({"source_version": self.source_metadata["version"],
                "index_sha256": self.source_metadata["index_sha256"]} if self.source_metadata else {}),
        }
        config_digest = hashlib.sha256(
            json.dumps(effective, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": "1",
            "config_sha256": config_digest,
            "system_id": self.system_id,
            "system_version": self.system_version,
            "agents": agent_configs,
            "agent_types": {key: agent.agent_type for key, agent in self.agents.items()},
            "protocol": self.protocol,
            "workflow_spec": self.workflow.model_dump(by_alias=True),
            "source": {
                **self.source.model_dump(),
                "resolved_path": str(self.source_path),
                "sha256": self.source_digest,
                **(self.source_metadata or {}),
            },
            "dataset": {
                "name": self.source.name,
                "source_id": self.source.source_id,
                "record_id": self.record.get("_id"),
            },
        }


def _path(base: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ValueError(f"配置路径必须是相对路径: {relative}")
    return (base / candidate).resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workflow(raw: dict[str, Any]) -> WorkflowSpec:
    rules = raw.get("routing_rules", {})
    if not isinstance(rules, dict) or not isinstance(rules.get("max_cycles"), dict):
        raise ValueError("协议 routing_rules.max_cycles 必须是对象")
    return WorkflowSpec.model_validate(
        {
            "protocol_id": raw.get("protocol_id"),
            "method_id": raw.get("method_id"),
            "agents": raw.get("agents"),
            "entry_node": raw.get("entry_node"),
            "edges": raw.get("edges"),
            "max_cycles": rules["max_cycles"],
        }
    )


def resolve_system(
    workflow_id: str | None = None,
    source_id: str | None = None,
    *,
    source_version: int | None = None,
    catalog_path: Path | str = DEFAULT_CATALOG_PATH,
) -> ResolvedSystem:
    catalog_file = Path(catalog_path).resolve()
    catalog = load_json(catalog_file)
    if catalog.get("schema_version") != "1":
        raise ValueError("不支持的系统配置 schema_version")
    if not isinstance(catalog.get("system_id"), str) or not catalog["system_id"].strip():
        raise ValueError("系统配置缺少 system_id")
    if not isinstance(catalog.get("system_version"), str) or not catalog["system_version"].strip():
        raise ValueError("系统配置缺少 system_version")
    workflow_id = workflow_id or catalog.get("default_workflow_id")
    source_id = source_id or catalog.get("default_source_id")
    workflows = catalog.get("workflows")
    if not isinstance(workflows, dict) or workflow_id not in workflows:
        raise ValueError(f"未知 workflow_id: {workflow_id}")
    if not isinstance(workflows[workflow_id], str):
        raise ValueError(f"workflow_id {workflow_id} 的路径必须是字符串")
    sources = catalog.get("sources")
    if not isinstance(sources, list):
        raise ValueError("系统 sources 必须是列表")
    if not all(isinstance(entry, dict) for entry in sources):
        raise ValueError("系统 sources 的每个条目必须是对象")
    selected = [entry for entry in sources if entry.get("source_id") == source_id]
    source_metadata = None
    if len(selected) > 1:
        raise ValueError(f"重复的 source_id: {source_id}")
    if selected:
        source = KnowledgeSourceSpec.model_validate(selected[0])
        if source.source_type != "hotpotqa_record" or source_version is not None:
            raise ValueError("预置知识源仅支持原有 HotpotQA 记录；用户文档需通过导入服务注册。")
        source_path = _path(catalog_file.parent, source.path)
        if not source_path.is_file():
            raise ValueError(f"知识源文件不存在: {source.path}")
        source_digest = _sha256(source_path)
        record = SOURCE_LOADERS[source.source_type](source_path)
        if _sha256(source_path) != source_digest:
            raise ValueError(f"知识源读取期间发生变化: {source_id}")
    else:
        source_metadata = KnowledgeStore().resolve(catalog["system_id"], source_id, source_version)
        source_path = Path(source_metadata["resolved_path"])
        source = KnowledgeSourceSpec(source_id=source_id, source_type="document_corpus",
                                    path=f"{source_id}/{source_metadata['version']}/corpus.json",
                                    name=source_metadata["name"])
        source_digest = source_metadata["sha256"]
        record = SOURCE_LOADERS["document_corpus"](source_path)
    protocol = load_json(_path(catalog_file.parent, workflows[workflow_id]))
    workflow = _workflow(protocol)
    if workflow.protocol_id != workflow_id:
        raise ValueError(f"协议 ID 不匹配: {workflow_id}")

    agents: dict[str, AgentSpec] = {}
    declared = catalog.get("agents")
    if not isinstance(declared, list):
        raise ValueError("系统 agents 必须是列表")
    for entry in declared:
        if not isinstance(entry, dict) or not all(
            key in entry for key in ("agent_id", "agent_type", "path")
        ):
            raise ValueError("系统 agents 的条目必须包含 agent_id、agent_type、path")
        config = load_json(_path(catalog_file.parent, entry["path"]))
        validate_required_fields(config, REQUIRED_AGENT_FIELDS, entry["agent_id"])
        agent = AgentSpec.model_validate(
            {"agent_id": entry["agent_id"], "agent_type": entry["agent_type"], "config": config}
        )
        if agent.agent_id in agents or config["agent_id"] != agent.agent_id:
            raise ValueError(f"重复或不匹配的 agent_id: {agent.agent_id}")
        if config["role"] != agent.agent_type:
            raise ValueError(f"agent_type 与 role 不一致: {agent.agent_id}")
        agents[agent.agent_id] = agent
    if set(workflow.agents) != set(agents):
        raise ValueError("协议 agents 与系统智能体配置不一致")
    registered_ids = {
        "planner": "planner_v0", "retriever": "retriever_v0",
        "answer_validator": "answer_validator_v0",
    }
    for agent in agents.values():
        if agent.agent_id != registered_ids[agent.agent_type]:
            raise ValueError(f"未注册的智能体节点: {agent.agent_id}")
    bindings = agents["retriever_v0"].config["knowledge_sources"]
    if not isinstance(bindings, list) or not all(isinstance(item, dict) for item in bindings):
        raise ValueError("retriever_v0.knowledge_sources 必须是对象列表")
    bound = {item.get("source_id") for item in bindings}
    if source_id not in bound and source_metadata is None:
        raise ValueError(f"知识源 {source_id} 未绑定到 retriever_v0")
    return ResolvedSystem(
        system_id=str(catalog["system_id"]),
        system_version=str(catalog["system_version"]),
        agents=agents,
        workflow=workflow,
        protocol=protocol,
        source=source,
        source_path=source_path,
        source_digest=source_digest,
        record=record,
        catalog_path=catalog_file,
        source_metadata=source_metadata,
    )


def retriever_from_snapshot(snapshot: dict[str, Any]) -> BM25Retriever:
    source = snapshot["source"]
    if source["source_type"] == "document_corpus":
        return retriever_from_corpus_snapshot(source)
    factory = RETRIEVER_FACTORIES.get(source["source_type"])
    if factory is None:
        raise ValueError(f"未知 source_type: {source['source_type']}")
    path = Path(source["resolved_path"])
    if _sha256(path) != source["sha256"]:
        raise ValueError(f"知识源内容在运行开始前发生变化: {source['source_id']}")
    return factory(SOURCE_LOADERS[source["source_type"]](path), source["source_id"])
