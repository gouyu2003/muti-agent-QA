"""Multi-Agent QA V0 的 FastAPI 入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager

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
from src.graph import build_graph
from src.knowledge.hotpotqa_loader import load_fixture


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用入口负责加载本地环境变量，不输出其中的值。
    load_dotenv()
    app.state.graph_factory = build_graph
    yield


app = FastAPI(
    title="Multi-Agent QA V0 API",
    version="0.1.0",
    lifespan=lifespan,
)

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
    graph_factory = getattr(request.app.state, "graph_factory", build_graph)
    return StreamingResponse(
        stream_qa_events(body.question, graph_factory=graph_factory),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
