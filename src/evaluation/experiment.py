"""Opt-in six-method experiments: prepare, preflight, run, resume, report.

Only run/resume with --allow-model-calls may contact a model. All paths in a
configuration file are relative to that file, not the terminal working directory.
"""
from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import signal
import sys
import threading
from time import monotonic
from uuid import uuid4
import zipfile

from pydantic import ValidationError

from src.evaluation.experiment_config import ROOT, ExperimentConfig, load_config, read_json, atomic_json, digest
from src.evaluation.experiment_data import prepare_data, selected_data
from src.evaluation.experiment_methods import method_definition, build_context
from src.evaluation.experiment_scores import score_detail
from src.evaluation.experiment_store import Ledger, experiment_lock, utcnow
from src.evaluation.experiment_report import generate_report
from src.evaluation.hotpotqa import sha256_file
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.version import source_fingerprint, code_version
from src.model_errors import REQUEST_RETRY_POLICY, request_errors


def identity():
    return dict(source_sha256=source_fingerprint(),
                templates={p.name: sha256_file(p) for p in sorted((ROOT / 'system-design/agents').glob('*.json'))},
                python=sys.version, dependencies={d.metadata['Name']: d.version for d in metadata.distributions() if d.metadata['Name']})


def provider_identity():
    from dotenv import load_dotenv
    from src.llm import require_env, ENV_API_KEY, ENV_BASE_URL
    load_dotenv(ROOT / '.env')
    require_env(ENV_API_KEY)
    # Only a digest of the endpoint is saved. Credentials never enter the archive.
    return digest(require_env(ENV_BASE_URL).rstrip('/'))


def preflight(config):
    records, manifest = selected_data(config)
    definitions = {m: method_definition(config, m) for m in config.methods}
    return dict(purpose=config.purpose, samples=len(records), methods=config.methods, repeats=config.repeats,
                planned_tasks=len(records) * len(config.methods) * config.repeats,
                model=config.model.model_dump(), limits=config.limits.model_dump(),
                reservation_note='UTF-8 byte estimate + maximum outputs; not actual tokens or a billing cap',
                dataset_sha256=manifest['dataset_sha256'], output=config.output,
                method_hashes={m: d['sha256'] for m, d in definitions.items()}, model_calls=0)


def archive_source(directory):
    files = []
    for folder in ('src', 'backend'):
        files.extend((ROOT / folder).rglob('*.py'))
    files.extend((ROOT / 'system-design/agents').glob('*.json'))
    files.extend(ROOT.glob('requirements*.txt'))
    files.append(ROOT / 'main.py')
    path = directory / 'source.zip'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files): archive.write(file, file.relative_to(ROOT).as_posix())
    return sha256_file(path)


def initialize(directory, config, records, manifest, definitions, provider):
    existing = [p.name for p in directory.iterdir() if p.name != '.execution.lock']
    if existing:
        raise ValueError('Output already exists; use resume or a new output directory')
    atomic_json(directory / 'config.resolved.json', config.model_dump())
    atomic_json(directory / 'manifest.json', manifest)
    for method, definition in definitions.items(): atomic_json(directory / 'methods' / f'{method}.json', definition)
    provenance = dict(created_at=utcnow(), identity=identity(), code_version=code_version(),
                      config_sha256=digest(config.model_dump()), manifest_sha256=digest(manifest),
                      method_hashes={m: digest(d) for m, d in definitions.items()},
                      source_archive_sha256=archive_source(directory), provider_sha256=provider,
                      execution='offline_test' if provider == 'offline_test' else 'real_model',
                      request_policy=dict(sdk_retries=0, transient_errors=dict(REQUEST_RETRY_POLICY), response_format='json_object',
                                          deepseek_thinking='disabled' if config.model.name.lower().startswith('deepseek') else 'not_sent'))
    atomic_json(directory / 'provenance.json', provenance)
    ledger = Ledger(directory)
    try: ledger.initialize(records, config)
    finally: ledger.close()
    return provenance


def load_frozen(directory, *, for_execution=False, provider=None):
    directory = Path(directory).resolve()
    provenance = read_json(directory / 'provenance.json')
    config = ExperimentConfig.model_validate(read_json(directory / 'config.resolved.json'))
    if digest(config.model_dump()) != provenance['config_sha256'] or Path(config.output).resolve() != directory:
        raise ValueError('Frozen config or experiment location changed; start a new experiment')
    records, manifest = selected_data(config)
    if digest(manifest) != provenance['manifest_sha256'] or read_json(directory / 'manifest.json') != manifest:
        raise ValueError('Frozen manifest changed')
    definitions = {m: read_json(directory / 'methods' / f'{m}.json') for m in config.methods}
    if {m: digest(d) for m, d in definitions.items()} != provenance['method_hashes']:
        raise ValueError('Frozen methods changed')
    if sha256_file(directory / 'source.zip') != provenance['source_archive_sha256']:
        raise ValueError('Source archive changed')
    if for_execution and (identity() != provenance['identity'] or provider != provenance['provider_sha256']):
        raise ValueError('Code, dependencies, template files or model endpoint changed; start a new experiment')
    return config, records, definitions, provenance


def unknown_result(task, run_id, record, detail=None):
    return dict(sample_id=task['sample_id'], method_id=task['method_id'], run_id=run_id,
                question=record['question'], type=record['type'], status='interrupted',
                metrics=detail.run.metrics.model_dump() if detail else {}, scores=None, labels=['interrupted_unknown'],
                gold={'answer': record['answer'], 'supporting_facts': record['supporting_facts']})


def collect(directory, ledger, records, repository, *, settle=False):
    index = {r['_id']: r for r in records}
    collected = []
    for attempt in ledger.attempts():
        detail = repository.get_run_detail(attempt['run_id'])
        if detail and (detail.run.context.sample_id != attempt['sample_id']
                       or detail.run.context.method_id != attempt['method_id']
                       or detail.run.context.experiment_id != directory.name):
            raise ValueError('Run identity does not match the task ledger')
        if not detail and attempt['status'] in ('completed', 'failed', 'cancelled'):
            raise ValueError('A terminal run is missing from the experiment database')
        if detail and detail.run.status in ('completed', 'failed', 'cancelled'):
            result = score_detail(detail, index[attempt['sample_id']])
            atomic_json(directory / 'runs' / f'{attempt["run_id"]}.json', detail.model_dump(mode='json'))
            if settle and attempt['status'] == 'running':
                ledger.finish(attempt['run_id'], detail.run.status, result)
        else:
            result = unknown_result(attempt, attempt['run_id'], index[attempt['sample_id']], detail)
            if settle and attempt['status'] == 'running': ledger.finish(attempt['run_id'], 'interrupted', result)
        collected.append({**attempt, 'result': result})
    return collected


def execute(config=None, *, directory=None, allow_model_calls=False, retry_status=(), service_factory=None):
    """service_factory is a private offline-test seam; the CLI never exposes it."""
    if not allow_model_calls and service_factory is None:
        raise ValueError('Real execution requires --allow-model-calls; preflight is offline')
    if not set(retry_status) <= {'failed', 'interrupted', 'cancelled'}:
        raise ValueError('Retry status must be failed,interrupted,cancelled; completed is never retried')
    provider = 'offline_test' if service_factory else provider_identity()
    is_new = config is not None
    if is_new:
        records, manifest = selected_data(config)
        definitions = {m: method_definition(config, m) for m in config.methods}
        directory = Path(config.output)
    else:
        directory = Path(directory).resolve()
        if not (directory / 'provenance.json').is_file(): raise ValueError('Not an initialized experiment')
    with experiment_lock(directory):
        if is_new:
            initialize(directory, config, records, manifest, definitions, provider)
        config, records, definitions, provenance = load_frozen(directory, for_execution=True, provider=provider)
        ledger = Ledger(directory, existing=True)
        try:
            ledger.validate(records, config)
            if ledger.attempts() and not (directory / 'runs.sqlite3').is_file():
                raise ValueError('Run database is missing; refusing to replay tasks')
        except Exception:
            ledger.close()
            raise
        repository = SQLiteRunRepository(directory / 'runs.sqlite3')
        service = (service_factory or RunService)(repository)
        index = {r['_id']: r for r in records}
        stopping = False
        interrupts = 0
        def stop(_signum, _frame):
            nonlocal stopping, interrupts
            stopping = True; interrupts += 1
            if interrupts > 1: raise KeyboardInterrupt
            print('Stop requested: finishing the current task; press Ctrl+C again to interrupt (remote charge may be unknown).', flush=True)
        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGINT, stop)
        started = monotonic()
        session = ledger.session_start()
        try:
            collect(directory, ledger, records, repository, settle=True)
            tasks = ledger.tasks()
            for i, task in enumerate(tasks):
                if stopping: break
                attempts = ledger.attempts(task['task_id'])
                if attempts and attempts[-1]['status'] not in retry_status: continue
                run_id = str(uuid4())
                context = build_context(definitions[task['method_id']], index[task['sample_id']],
                                        directory, directory.name, run_id, provenance['code_version'])
                number = ledger.start(task, run_id)
                try:
                    detail = service.run(context)
                except KeyboardInterrupt:
                    stopping = True
                    collect(directory, ledger, records, repository, settle=True)
                    break
                except Exception:
                    # Infrastructure failures cannot be treated as answered questions or silently retried.
                    collect(directory, ledger, records, repository, settle=True)
                    raise
                result = score_detail(detail, index[task['sample_id']])
                atomic_json(directory / 'runs' / f'{run_id}.json', detail.model_dump(mode='json'))
                ledger.finish(run_id, detail.run.status, result)
                event = dict(at=utcnow(), progress=f'{i + 1}/{len(tasks)}', method=task['method_id'],
                             sample_id=task['sample_id'], repeat=task['repeat'], attempt=number,
                             status=detail.run.status, duration_ms=detail.run.metrics.duration_ms,
                             tokens=detail.run.metrics.total_tokens)
                if detail.run.error:
                    event['error'] = detail.run.error.model_dump()
                errors = request_errors(detail.run.metrics.agent_usage)
                if errors:
                    event['request_errors'] = errors
                (directory / 'logs').mkdir(exist_ok=True)
                with (directory / 'logs' / 'progress.jsonl').open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + '\n')
                print(json.dumps(event, ensure_ascii=False), flush=True)
        finally:
            if previous is not None: signal.signal(signal.SIGINT, previous)
            ledger.session_finish(session, monotonic() - started)
            ledger.close()
    return report(directory)


def report(directory):
    directory = Path(directory).resolve()
    if not (directory / 'provenance.json').is_file(): raise ValueError('Not an initialized experiment')
    with experiment_lock(directory):
        config, records, definitions, provenance = load_frozen(directory)
        ledger = Ledger(directory, existing=True)
        try:
            ledger.validate(records, config)
            if ledger.attempts() and not (directory / 'runs.sqlite3').is_file():
                raise ValueError('Run database is missing')
            repository = SQLiteRunRepository(directory / 'runs.sqlite3')
            attempts = collect(directory, ledger, records, repository)
            return generate_report(directory, config, records, ledger, attempts, provenance)
        finally: ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'preflight', 'run'):
        part = sub.add_parser(name)
        part.add_argument('--config', type=Path, required=True)
        if name == 'run': part.add_argument('--allow-model-calls', action='store_true')
    for name in ('resume', 'report'):
        part = sub.add_parser(name)
        part.add_argument('--experiment', type=Path, required=True)
        if name == 'resume':
            part.add_argument('--allow-model-calls', action='store_true')
            part.add_argument('--retry-status', default='')
    args = parser.parse_args(argv)
    try:
        if args.command in ('run', 'resume') and not args.allow_model_calls:
            raise ValueError('Real execution requires --allow-model-calls')
        if args.command in ('prepare', 'preflight', 'run'):
            config = load_config(args.config)
            if args.command == 'prepare': value = prepare_data(config)
            elif args.command == 'preflight': value = preflight(config)
            else: value = str(execute(config, allow_model_calls=True))
        elif args.command == 'resume':
            value = str(execute(directory=args.experiment, allow_model_calls=True,
                                retry_status=tuple(x for x in args.retry_status.split(',') if x)))
        else: value = str(report(args.experiment))
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except ValidationError as exc:
        print('Invalid configuration: ' + ', '.join('.'.join(map(str, e['loc'])) for e in exc.errors()), file=sys.stderr)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
