"""Multi-Agent QA V0 的 FastAPI 入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Callable

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from backend.schemas import (
    AgentInfo,
    DatasetInfo,
    FlowEdge,
    HealthResponse,
    QuestionRequest,
    SystemInfoResponse,
)
from backend.streaming import stream_qa_events
from src.config_loader import load_agent_configs, load_protocol_config
from src.knowledge.hotpotqa_loader import load_fixture
from src.runtime.context import build_v0_run_context
from src.runtime.models import RunContext
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository


RunServiceFactory = Callable[[], RunService]


def build_default_run_service() -> RunService:
    """构造生产环境默认的持久化运行服务。"""

    return RunService(SQLiteRunRepository())


def build_api_run_context(question: str, run_id: str | None = None) -> RunContext:
    """根据当前 V0 配置生成一次 API 运行的可复现实验上下文。"""

    return build_v0_run_context(
        question,
        entrypoint="api",
        run_id=run_id,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用入口负责加载本地环境变量，不输出其中的值。
    load_dotenv()
    app.state.run_service = app.state.run_service_factory()
    try:
        yield
    finally:
        del app.state.run_service


app = FastAPI(
    title="Multi-Agent QA V0 API",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.run_service_factory = build_default_run_service

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/api/v1/system", response_model=SystemInfoResponse)
def system_info() -> SystemInfoResponse:
    agents = load_agent_configs()
    protocol = load_protocol_config()
    fixture = load_fixture()

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
            condition=edge["condition"],
        )
        for edge in protocol["edges"]
    ]
    first_agent = next(iter(agents.values()))

    return SystemInfoResponse(
        name="Multi-Agent QA",
        version="v0",
        protocol_id=protocol["protocol_id"],
        model_name=first_agent["model_config"]["model_name"],
        agents=agent_items,
        flow=flow,
        retry_limits={
            key: int(value)
            for key, value in protocol["routing_rules"]["max_cycles"].items()
        },
        dataset=DatasetInfo(
            name="HotpotQA",
            record_id=fixture["_id"],
            question_type=fixture["type"],
            level=fixture.get("level", "unknown"),
            context_count=len(fixture["context"]),
            supporting_fact_count=len(fixture["supporting_facts"]),
            default_question=fixture["question"],
        ),
    )


@app.post("/api/v1/qa/stream")
def stream_question(body: QuestionRequest, request: Request) -> StreamingResponse:
    run_service: RunService = request.app.state.run_service
    context = build_api_run_context(body.question)
    return StreamingResponse(
        stream_qa_events(context, run_service),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
