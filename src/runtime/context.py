"""为 API、CLI 和后续批处理构造统一的 V0 运行上下文。"""

from __future__ import annotations

import os
from uuid import uuid4

from src.config_loader import load_agent_configs, load_protocol_config
from src.knowledge.hotpotqa_loader import load_fixture
from src.runtime.models import RunContext, RunEntryPoint


SYSTEM_VERSION = "v0"
METHOD_ID = "multi_agent_adaptive_v0"
FIXTURE_SOURCE_ID = "hotpotqa_fixture"


def build_v0_run_context(
    question: str,
    *,
    entrypoint: RunEntryPoint,
    run_id: str | None = None,
) -> RunContext:
    """根据当前静态配置生成一次可持久化、可复现的运行上下文。"""

    agents = load_agent_configs()
    protocol = load_protocol_config()
    fixture = load_fixture()
    first_agent = next(iter(agents.values()))

    return RunContext(
        run_id=run_id or str(uuid4()),
        entrypoint=entrypoint,
        question=question,
        source_id=FIXTURE_SOURCE_ID,
        record_id=fixture["_id"],
        workflow_id=protocol["protocol_id"],
        method_id=METHOD_ID,
        model_name=first_agent["model_config"]["model_name"],
        protocol_id=protocol["protocol_id"],
        code_version=os.getenv("MAQA_CODE_VERSION", SYSTEM_VERSION),
        config_snapshot={
            "agents": agents,
            "protocol": protocol,
            "dataset": {
                "name": "HotpotQA",
                "source_id": FIXTURE_SOURCE_ID,
                "record_id": fixture["_id"],
            },
        },
    )
