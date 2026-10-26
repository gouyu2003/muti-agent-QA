"""Rebuild at most two failed smoke requests; only --allow-model-calls sends them.

Run from the project root. The default preview is offline and writes no files.
Real probes make ONE SDK request per failed node, with no automatic retries.
They write separate, redacted diagnostics, never experiment results or answers.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import UUID, uuid4

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.evaluation.experiment import identity, load_frozen, provider_identity
from src.evaluation.experiment_config import read_json
from src.llm import build_chat_model, ENV_API_KEY, ENV_BASE_URL
from src.llm_usage import extract_token_usage
from src.model_errors import error_diagnostic, redact
from src.runtime.protocols import parse_protocol
from src.runtime.system_store import fingerprint


@dataclass
class Probe:
    metadata: dict
    config: dict
    prompt: str


class _PromptCaptured(Exception):
    pass


def reconstruct(detail: dict) -> Probe:
    """Use the original phase code, replacing model construction and invocation."""
    import src.agents.answer_validator as validator
    import src.runtime.protocol_agents as agents
    from src.runtime.protocol_runtime import execute_phase

    run = detail['run']
    context = run['context']
    snapshot = context['config_snapshot']
    # RunContext adds this bookkeeping field after the method snapshot is hashed.
    run_metadata = dict(experiment_id=context['experiment_id'], sample_id=context['sample_id'])
    snapshot_body = {key: value for key, value in snapshot.items() if key != '_run_metadata'}
    if (run['status'] != 'failed' or snapshot.get('_run_metadata') != run_metadata
            or fingerprint(snapshot_body) != snapshot['config_sha256']):
        raise ValueError('A valid frozen failed run is required')
    failed = [c for c in run['final_state'].get('protocol_calls', {}).values() if c['status'] == 'failed']
    if len(failed) != 1:
        raise ValueError('Expected exactly one failed protocol node')
    call = failed[0]
    attempts = run['metrics']['agent_usage'][call['agent_id']]['calls']
    if not any(c.get('outcome') == 'request_error' for c in attempts):
        raise ValueError('Selected failure is not a model request error')
    stage = next(s for s in parse_protocol(snapshot['protocol_document']['spec']).stages
                 if s.stage_id == call['stage_id'])
    if stage.kind not in ('review', 'critique'):
        raise ValueError('This diagnostic supports failed review/critique nodes only')
    source = snapshot.get('sources', {}).get(call['agent_id'])
    if source and source['source_type'] != 'hotpotqa_context':
        raise ValueError('Only local HotpotQA context sources are supported')
    config = deepcopy(snapshot['agents'][call['agent_id']])
    config['_request_timeout'] = 120
    captured = []

    def capture(_chat, prompt, *_args, **_kwargs):
        captured.append(prompt)
        raise _PromptCaptured()

    # Capture before any client can be constructed; restore every patch on exit.
    with ExitStack() as stack:
        stack.enter_context(patch.object(validator, 'default_llm', lambda *_a, **_k: None))
        stack.enter_context(patch.object(validator, 'invoke_json', capture))
        stack.enter_context(patch.object(agents, 'invoke_json', capture))
        try:
            execute_phase(stage, call['agent_id'], config, source, deepcopy(call['input']), None)
        except _PromptCaptured:
            pass
    if len(captured) != 1:
        raise ValueError('Could not reconstruct exactly one model request')
    prompt = captured[0]
    return Probe(dict(original_run_id=run['context']['run_id'], method=run['context']['method_id'],
                      sample_id=run['context']['sample_id'], stage=stage.kind, agent_id=call['agent_id'],
                      model=config['model_config']['model_name'],
                      reconstruction='from_persisted_node_input; original wire prompt was not archived',
                      prompt_sha256=hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                      prompt_chars=len(prompt), prompt_utf8_bytes=len(prompt.encode('utf-8'))), config, prompt)


def prepare(experiment: Path, run_ids=None):
    experiment = experiment.resolve()
    config, _records, definitions, provenance = load_frozen(experiment)
    if config.purpose != 'smoke' or provenance['execution'] != 'real_model':
        raise ValueError('Select an existing real-model smoke experiment')
    if identity() != provenance['identity']:
        raise ValueError('Runtime source, templates, Python or dependencies changed; reconstruction refused')
    rows = [json.loads(line) for line in (experiment / 'results.jsonl').read_text(encoding='utf-8').splitlines()]
    eligible = {r['run_id']: r for r in rows if r['attempt'] == 1 and r['status'] == 'failed'}
    selected = list(eligible) if run_ids is None else run_ids
    if not 1 <= len(selected) <= 2 or len(set(selected)) != len(selected):
        raise ValueError('Select one or two distinct failed run IDs; maximum two requests per invocation')
    probes = []
    for run_id in selected:
        if str(UUID(run_id)) != run_id or run_id not in eligible:
            raise ValueError('Run ID is not a first-attempt failure in this experiment')
        detail = read_json(experiment / 'runs' / f'{run_id}.json')
        context = detail['run']['context']
        if (context['run_id'] != run_id or context['experiment_id'] != experiment.name
                or context['sample_id'] != eligible[run_id]['sample_id']
                or context['method_id'] != eligible[run_id]['method_id']
                or context['code_version'] != provenance['code_version']):
            raise ValueError('Run export identity does not match the frozen experiment')
        snapshot = context['config_snapshot']
        if snapshot['experiment_derivation']['method_sha256'] != definitions[context['method_id']]['sha256']:
            raise ValueError('Run method does not match the frozen method')
        probes.append(reconstruct(detail))
    return probes, provenance


def send_once(probe: Probe, secrets=()):
    """Exactly one invocation; no output repair, graph execution, or SDK retry."""
    started = datetime.now(timezone.utc).isoformat()
    model_calls = 0
    try:
        chat = build_chat_model(deepcopy(probe.config))
        if chat.max_retries != 0:
            raise ValueError('Diagnostics require SDK max_retries=0')
        if hasattr(chat, 'include_response_headers'):
            chat.include_response_headers = True
        model_calls = 1
        response = chat.invoke(probe.prompt, timeout=120)
    except Exception as exc:
        return {**probe.metadata, 'started_at': started, 'outcome': 'request_error',
                'error': error_diagnostic(exc, (*secrets, probe.prompt)), 'model_calls': model_calls}
    prompt_tokens, completion_tokens, total_tokens = extract_token_usage(response)
    metadata = getattr(response, 'response_metadata', None) or {}
    headers = metadata.get('headers') or {}
    request_id = headers.get('x-request-id') or headers.get('x-dashscope-request-id') or headers.get('x-acs-request-id')
    return {**probe.metadata, 'started_at': started, 'outcome': 'request_succeeded', 'model_calls': 1,
            'request_id': redact(request_id, secrets) if request_id else None,
            'finish_reason': metadata.get('finish_reason'),
            'usage': dict(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=total_tokens),
            'note': 'Transport success only; no answer/schema validation and no change to historical results.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, default=ROOT / 'data/experiments/baseline-smoke-001')
    parser.add_argument('--run-id', action='append', help='Select one original failed run; repeat at most twice')
    parser.add_argument('--allow-model-calls', action='store_true', help='Send each previewed request once; may incur charges')
    args = parser.parse_args(argv)
    try:
        probes, provenance = prepare(args.experiment, args.run_id)
        preview = dict(mode='request_plan' if args.allow_model_calls else 'offline_preview',
                       model_calls=0, maximum_model_calls=len(probes),
                       source_sha256=provenance['identity']['source_sha256'], probes=[p.metadata for p in probes])
        print(json.dumps(preview, ensure_ascii=False, indent=2), flush=True)
        if not args.allow_model_calls:
            return 0
        if provider_identity() != provenance['provider_sha256']:
            raise ValueError('Model endpoint changed; diagnostic requests refused')
        secrets = (os.environ.get(ENV_API_KEY, '').strip(), os.environ.get(ENV_BASE_URL, '').strip())
        directory = ROOT / 'data/experiments/diagnostics'
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        path = directory / f'{args.experiment.resolve().name}-{stamp}-{uuid4().hex[:8]}.jsonl'
        errors = 0
        with path.open('x', encoding='utf-8') as handle:
            for probe in probes:
                result = send_once(probe, secrets)
                errors += result['outcome'] == 'request_error'
                result.update(kind='new_diagnostic_request', sdk_retries=0)
                line = json.dumps(result, ensure_ascii=False)
                handle.write(line + '\n'); handle.flush(); os.fsync(handle.fileno())
                print(line, flush=True)
        print(json.dumps(dict(diagnostics=str(path.resolve()), request_errors=errors,
                              note='New responses cannot recover the original historical HTTP error body.'), ensure_ascii=False), flush=True)
        return 1 if errors else 0
    except Exception as exc:
        secrets = (os.environ.get(ENV_API_KEY, '').strip(), os.environ.get(ENV_BASE_URL, '').strip())
        print('Diagnostic precheck failed: ' + redact(str(exc) or type(exc).__name__, secrets), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Diagnostic interrupted. An in-flight request may still be billed; do not assume it was cancelled.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    raise SystemExit(main())
