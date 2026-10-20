"""Resolve a selected system once for API, CLI, and future batch runs."""

from __future__ import annotations

import os
from uuid import uuid4

from src.runtime.models import RunContext, RunEntryPoint
from src.runtime.specs import RuntimeRequest
from src.runtime.system import DEFAULT_CATALOG_PATH, resolve_system
from src.runtime.version import code_version


def build_v0_run_context(
    question: str,
    *,
    entrypoint: RunEntryPoint,
    run_id: str | None = None,
    workflow_id: str | None = None,
    source_id: str | None = None,
    source_version: int | None = None,
    catalog_path: str | None = None,
    system_id: str | None = None,
    system_version: str | None = None,
    draft_revision: int | None = None,
) -> RunContext:
    """Build a run context from one validated system and source snapshot."""

    request = RuntimeRequest(question=question, workflow_id=workflow_id, source_id=source_id, source_version=source_version,
                             system_id=system_id, system_version=system_version, draft_revision=draft_revision)
    if request.system_id:
        if catalog_path is not None:
            raise ValueError('系统版本运行不能覆盖系统配置文件。')
        from src.runtime.system_store import SystemStore
        snapshot = SystemStore().run_snapshot(request.system_id, request.system_version, request.draft_revision)
        workflow = snapshot['workflow_spec']
        models = {agent['model_config']['model_name'] for agent in snapshot['agents'].values()
                  if agent.get('model_config', {}).get('model_name')}
        return RunContext(run_id=run_id or str(uuid4()), entrypoint=entrypoint, question=request.question,
                          source_id=snapshot.get('source', {}).get('source_id'), record_id=snapshot.get('dataset', {}).get('record_id'),
                          workflow_id=workflow['protocol_id'], method_id=workflow['method_id'],
                          model_name=next(iter(models)) if len(models) == 1 else 'multiple', protocol_id=workflow['protocol_id'],
                          code_version=code_version(), config_snapshot=snapshot)
    system = resolve_system(
        request.workflow_id, request.source_id,
        **({"source_version": request.source_version} if request.source_version is not None else {}),
        catalog_path=catalog_path or os.getenv("MAQA_SYSTEM_CONFIG_PATH") or DEFAULT_CATALOG_PATH,
    )
    first_agent = next(iter(system.agents.values())).config

    return RunContext(
        run_id=run_id or str(uuid4()),
        entrypoint=entrypoint,
        question=request.question,
        source_id=system.source.source_id,
        record_id=system.record.get("_id"),
        workflow_id=system.workflow.protocol_id,
        method_id=system.workflow.method_id,
        model_name=first_agent["model_config"]["model_name"],
        protocol_id=system.workflow.protocol_id,
        code_version=code_version(),
        config_snapshot=system.snapshot(),
    )
