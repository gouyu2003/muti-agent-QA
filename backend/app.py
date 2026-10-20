"""Multi-Agent QA V0 的 FastAPI 入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import os
from typing import Callable

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from backend.schemas import (
    AgentInfo,
    DatasetInfo,
    FlowEdge,
    HealthResponse,
    QuestionRequest,
    RunListResponse,
    SystemInfoResponse,
    SystemOptionsResponse,
    WorkflowOption,
    SourceOption,
)
from src.config_loader import load_json
from backend.streaming import stream_qa_events
from backend.knowledge_api import router as knowledge_router
from backend.systems_api import router as systems_router
from backend.systems_api import templates_router
from src.runtime.system_store import SystemStore, SystemFailure
from src.knowledge.store import KnowledgeStore
from src.knowledge.corpus import ImportFailure
from src.runtime.context import build_v0_run_context
from src.runtime.models import (
    RunContext,
    RunDetail,
    RunEntryPoint,
    RunStatsSummary,
    RunStatus,
    TraceEvent,
)
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.system import DEFAULT_CATALOG_PATH, resolve_system
from src.runtime.history import HistoryReader, ExperimentOption, summarize_history
from src.runtime.repository import RepositoryError


RunServiceFactory = Callable[[], RunService]


def build_default_run_service() -> RunService:
    """构造生产环境默认的持久化运行服务。"""

    return RunService(SQLiteRunRepository())


def build_api_run_context(
    question: str, run_id: str | None = None,
    *, workflow_id: str | None = None, source_id: str | None = None,
    source_version: int | None = None,
    system_id: str | None = None, system_version: str | None = None, draft_revision: int | None = None,
) -> RunContext:
    """根据当前 V0 配置生成一次 API 运行的可复现实验上下文。"""

    return build_v0_run_context(
        question,
        entrypoint="api",
        **({"system_id": system_id, "system_version": system_version, "draft_revision": draft_revision} if system_id else {}),
        run_id=run_id,
        workflow_id=workflow_id,
        source_id=source_id,
        **({"source_version": source_version} if source_version is not None else {}),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用入口负责加载本地环境变量，不输出其中的值。
    load_dotenv()
    app.state.run_service = app.state.run_service_factory()
    app.state.system_store = SystemStore()
    app.state.knowledge_store = KnowledgeStore()
    app.state.knowledge_store.recover_interrupted()
    try:
        yield
    finally:
        del app.state.run_service
        del app.state.knowledge_store
        del app.state.system_store


app = FastAPI(
    title="Multi-Agent QA V0 API",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.run_service_factory = build_default_run_service
app.include_router(knowledge_router)
app.include_router(systems_router)
app.include_router(templates_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)


def aware_started_from(started_from: datetime | None = None) -> datetime | None:
    if started_from is not None and started_from.utcoffset() is None:
        raise HTTPException(status_code=422, detail="started_from 必须包含时区")
    return started_from


def history_reader(request: Request) -> HistoryReader:
    return HistoryReader(request.app.state.run_service.repository)


@app.exception_handler(SystemFailure)
async def system_failure_handler(_request: Request, exc: SystemFailure):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.exception_handler(RepositoryError)
async def history_repository_error(_request: Request, _exc: RepositoryError):
    return JSONResponse(status_code=503, content={"detail": "运行档案读取失败，请检查实验档案是否已完成并可用。"})


@app.get('/api/v1/experiments', response_model=list[ExperimentOption])
def list_experiments(request: Request):
    return history_reader(request).experiments()


@app.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    system = resolve_system(
        catalog_path=os.getenv("MAQA_SYSTEM_CONFIG_PATH") or DEFAULT_CATALOG_PATH
    )
    return HealthResponse(system_version=system.system_version)


@app.get("/api/v1/system", response_model=SystemInfoResponse)
def system_info() -> SystemInfoResponse:
    system = resolve_system(
        catalog_path=os.getenv("MAQA_SYSTEM_CONFIG_PATH") or DEFAULT_CATALOG_PATH
    )
    agents = {key: agent.config for key, agent in system.agents.items()}
    protocol = system.protocol
    fixture = system.record

    agent_items = [
        AgentInfo(
            agent_id=agent_id,
            name=config["agent_name"],
            role=config["role"],
            description=config.get("description", ""),
        )
        for agent_id, config in agents.items()
    ]
    flow = [
        FlowEdge(
            source=edge["from"],
            target=edge["to"],
            condition=edge.get("condition", edge["route_name"]),
        )
        for edge in protocol["edges"]
    ]
    first_agent = next(iter(agents.values()))

    return SystemInfoResponse(
        name="Multi-Agent QA",
        version=system.system_version,
        protocol_id=protocol["protocol_id"],
        model_name=first_agent["model_config"]["model_name"],
        agents=agent_items,
        flow=flow,
        retry_limits={
            key: int(value)
            for key, value in protocol["routing_rules"]["max_cycles"].items()
        },
        dataset=DatasetInfo(
            name=system.source.name,
            record_id=fixture["_id"],
            question_type=fixture["type"],
            level=fixture.get("level", "unknown"),
            context_count=len(fixture["context"]),
            supporting_fact_count=len(fixture["supporting_facts"]),
            default_question=fixture["question"],
        ),
    )


@app.get("/api/v1/system/options", response_model=SystemOptionsResponse)
def system_options() -> SystemOptionsResponse:
    """Expose only validated, display-safe choices from the active catalog."""
    catalog_path = os.getenv("MAQA_SYSTEM_CONFIG_PATH") or DEFAULT_CATALOG_PATH
    catalog = load_json(catalog_path)
    default_workflow_id = catalog.get("default_workflow_id")
    default_source_id = catalog.get("default_source_id")
    workflows = catalog.get("workflows")
    sources = catalog.get("sources")
    if not isinstance(workflows, dict) or not isinstance(sources, list):
        raise HTTPException(status_code=503, detail="系统配置不可用")
    source_ids = [entry.get("source_id") for entry in sources if isinstance(entry, dict)]
    if (len(source_ids) != len(sources) or not workflows or not sources
            or any(not isinstance(source_id, str) or not source_id for source_id in source_ids)
            or len(set(source_ids)) != len(source_ids)
            or default_workflow_id not in workflows or default_source_id not in source_ids):
        raise HTTPException(status_code=503, detail="系统配置不可用")

    try:
        resolved = {
            (workflow_id, source_id): resolve_system(
                workflow_id, source_id, catalog_path=catalog_path
            )
            for workflow_id in workflows
            for source_id in source_ids
        }
        default = resolved[(default_workflow_id, default_source_id)]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="系统配置不可用") from exc

    workflow_options = []
    for workflow_id in workflows:
        system = resolved[(workflow_id, default_source_id)]
        spec = system.workflow
        workflow_options.append(WorkflowOption(
            workflow_id=workflow_id,
            name=system.protocol.get("protocol_name", workflow_id),
            description=system.protocol.get("description", ""),
            method_id=spec.method_id,
            entry_node=spec.entry_node,
            agents=spec.agents,
            edges=[
                {"source": edge.source, "target": edge.target,
                 "route_name": edge.route_name}
                for edge in spec.edges
            ],
            retry_limits=spec.max_cycles,
        ))

    source_options = []
    for source_id in source_ids:
        system = resolved[(default_workflow_id, source_id)]
        record = system.record
        source_options.append(SourceOption(
            source_id=source_id,
            name=system.source.name,
            source_type=system.source.source_type,
            is_demo=system.source.is_demo,
            record_id=record["_id"],
            question_type=record["type"],
            level=record.get("level", "unknown"),
            context_count=len(record["context"]),
            supporting_fact_count=len(record["supporting_facts"]),
            example_question=record["question"],
        ))
    store = KnowledgeStore()
    for item in store.list_sources(default.system_id):
        if item["current_version"] is None:
            continue
        try:
            store.resolve(default.system_id, item["source_id"])
        except ImportFailure:
            continue
        source_options.append(SourceOption(
            source_id=item["source_id"], name=item["name"], source_type="document_corpus", is_demo=False,
            version=item["current_version"],
            versions=[v["version"] for v in item["versions"] if v["status"] == "ready"],
            file_count=item["file_count"], chunk_count=item["chunk_count"],
            version_details=[{"version": v["version"], "file_count": v["file_count"], "chunk_count": v["chunk_count"]}
                             for v in item["versions"] if v["status"] == "ready"],
        ))
    return SystemOptionsResponse(
        system_id=default.system_id,
        system_version=default.system_version,
        default_workflow_id=default_workflow_id,
        default_source_id=default_source_id,
        workflows=workflow_options,
        sources=source_options,
    )


@app.post("/api/v1/qa/stream")
def stream_question(body: QuestionRequest, request: Request) -> StreamingResponse:
    run_service: RunService = request.app.state.run_service
    try:
        context = build_api_run_context(
            body.question, workflow_id=body.workflow_id, source_id=body.source_id,
            **({"system_id": body.system_id, "system_version": body.system_version, "draft_revision": body.draft_revision} if body.system_id else {}),
            **({"source_version": body.source_version} if body.source_version is not None else {}),
        )
    except SystemFailure as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return StreamingResponse(
        stream_qa_events(context, run_service),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/v1/runs", response_model=RunListResponse)
def list_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: RunStatus | None = None,
    entrypoint: RunEntryPoint | None = None,
    method_id: str | None = Query(default=None, min_length=1),
    started_from: datetime | None = Depends(aware_started_from),
    experiment_id: str | None = Query(default=None, min_length=1),
    system_id: str | None = Query(default=None, min_length=1),
) -> RunListResponse:
    reader = history_reader(request)
    filters = {
        "status": status,
        "entrypoint": entrypoint,
        "method_id": method_id,
        "started_from": started_from,
        "experiment_id": experiment_id,
        "system_id": system_id,
    }
    records = reader.records(**filters)
    return RunListResponse(
        items=records[offset:offset + limit],
        total=len(records),
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/runs/{run_id}", response_model=RunDetail)
def get_run_detail(run_id: str, request: Request,
                   experiment_id: str | None = Query(default=None, min_length=1)) -> RunDetail:
    detail = history_reader(request).detail(run_id, experiment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return detail


@app.get("/api/v1/runs/{run_id}/events", response_model=list[TraceEvent])
def get_run_events(run_id: str, request: Request,
                   experiment_id: str | None = Query(default=None, min_length=1)) -> list[TraceEvent]:
    detail = history_reader(request).detail(run_id, experiment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return detail.events


@app.get("/api/v1/stats/summary", response_model=RunStatsSummary)
def get_stats_summary(
    request: Request,
    status: RunStatus | None = None,
    entrypoint: RunEntryPoint | None = None,
    method_id: str | None = Query(default=None, min_length=1),
    started_from: datetime | None = Depends(aware_started_from),
    experiment_id: str | None = Query(default=None, min_length=1),
    system_id: str | None = Query(default=None, min_length=1),
) -> RunStatsSummary:
    return summarize_history(history_reader(request).records(
        status=status, entrypoint=entrypoint, method_id=method_id,
        started_from=started_from,
        experiment_id=experiment_id, system_id=system_id,
    ))


@app.get(
    "/api/v1/exports/runs.jsonl",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "按筛选条件导出的 UTF-8 JSONL 文件",
            "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
        }
    },
)
def export_runs_jsonl(
    request: Request,
    status: RunStatus | None = None,
    entrypoint: RunEntryPoint | None = None,
    method_id: str | None = Query(default=None, min_length=1),
    started_from: datetime | None = Depends(aware_started_from),
    experiment_id: str | None = Query(default=None, min_length=1),
    system_id: str | None = Query(default=None, min_length=1),
) -> StreamingResponse:
    lines = history_reader(request).export(
        status=status, entrypoint=entrypoint, method_id=method_id,
        started_from=started_from,
        experiment_id=experiment_id, system_id=system_id,
    )
    return StreamingResponse(
        lines,
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="runs.jsonl"'},
    )
