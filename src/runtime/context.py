"""Resolve a selected system once for API, CLI, and future batch runs."""

from __future__ import annotations

import os
from uuid import uuid4

from src.runtime.models import RunContext, RunEntryPoint
from src.runtime.specs import RuntimeRequest
from src.runtime.system import DEFAULT_CATALOG_PATH, resolve_system


SYSTEM_VERSION = "v0"


def build_v0_run_context(
    question: str,
    *,
    entrypoint: RunEntryPoint,
    run_id: str | None = None,
    workflow_id: str | None = None,
    source_id: str | None = None,
    catalog_path: str | None = None,
) -> RunContext:
    """Build a run context from one validated system and source snapshot."""

    request = RuntimeRequest(question=question, workflow_id=workflow_id, source_id=source_id)
    system = resolve_system(
        request.workflow_id, request.source_id,
        catalog_path=catalog_path or os.getenv("MAQA_SYSTEM_CONFIG_PATH") or DEFAULT_CATALOG_PATH,
    )
    first_agent = next(iter(system.agents.values())).config

    return RunContext(
        run_id=run_id or str(uuid4()),
        entrypoint=entrypoint,
        question=request.question,
        source_id=system.source.source_id,
        record_id=system.record["_id"],
        workflow_id=system.workflow.protocol_id,
        method_id=system.workflow.method_id,
        model_name=first_agent["model_config"]["model_name"],
        protocol_id=system.workflow.protocol_id,
        code_version=os.getenv("MAQA_CODE_VERSION", SYSTEM_VERSION),
        config_snapshot=system.snapshot(),
    )
