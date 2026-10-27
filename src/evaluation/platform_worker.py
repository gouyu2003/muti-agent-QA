"""Detached evaluation worker. Only an explicit start request may launch this process."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

from dotenv import load_dotenv

from src.evaluation.experiment import archive_source, identity
from src.evaluation.experiment_config import ROOT, atomic_json, digest
from src.evaluation.experiment_methods import build_context
from src.evaluation.experiment_scores import score_detail
from src.evaluation.experiment_report import percentile
from src.evaluation.experiment_store import Ledger, experiment_lock
from src.evaluation.metrics import answer_scores
from src.evaluation.platform_store import EvaluationFailure, EvaluationStore
from src.llm import ENV_API_KEY, ENV_BASE_URL, require_env
from src.runtime.models import RunContext
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.version import code_version
from src.model_errors import request_errors


def ledger_config(frozen):
    return SimpleNamespace(methods=list(frozen['definitions']), repeats=frozen['repeats'], rotate_methods=True)


def result_for(detail, record, contextual):
    """Labels are read only after Runtime has returned."""
    if contextual and record['answer'] is not None and record.get('supporting_facts'):
        return score_detail(detail, record)
    state = detail.run.final_state or {}
    answer = state.get('answer') or ''
    scores = None
    if record['answer'] is not None:
        scores = answer_scores(answer, record['answer']) if detail.run.status == 'completed' else dict(em=0.0, f1=0.0)
    labels = ['runtime_' + detail.run.status] if detail.run.status != 'completed' else []
    if scores is not None and not scores['em']:
        labels.append('answer_mismatch')
    if state.get('failure_type') == 'output_error':
        labels.append('structured_output_error')
    labels.extend('provider_' + e['category'] for e in request_errors(detail.run.metrics.agent_usage))
    return dict(sample_id=record['_id'], question=record['question'], gold=record['answer'], type=record['type'],
                run_id=detail.run.context.run_id, method_id=detail.run.context.method_id, status=detail.run.status,
                answer=answer, supporting_evidence=state.get('supporting_evidence', []), scores=scores,
                metrics=detail.run.metrics.model_dump(), labels=sorted(set(labels)),
                error=detail.run.error.model_dump() if detail.run.error else None)


def reconcile(store, job_id):
    """A released OS lock proves the old writer is gone; unknown requests are never replayed here."""
    row, _ = store.frozen(job_id)
    if row['status'] not in ('queued', 'running', 'stopping'):
        return
    if row['status'] == 'queued' and (datetime.now(timezone.utc) - datetime.fromisoformat(row['updated_at'])).total_seconds() < 30:
        return
    try:
        with experiment_lock(store.job_directory(job_id)):
            store.state(job_id, 'interrupted', '执行进程已退出。恢复时将核对已保存终态，不自动重发状态不明的请求。')
    except ValueError:
        pass  # The worker is still holding the lock.


def start_job(store, job_id, *, allow_model_calls, retry_failed=False):
    if not allow_model_calls:
        raise EvaluationFailure('启动会调用模型，需明确确认 allow_model_calls。')
    for job in store.jobs():
        reconcile(store, job['job_id'])
    load_dotenv(ROOT / '.env')
    try:
        require_env(ENV_API_KEY)
        require_env(ENV_BASE_URL)
    except RuntimeError as exc:
        raise EvaluationFailure('请先在后端环境配置模型 API Key 和 Base URL。') from exc
    try:
        with experiment_lock(store.job_directory(job_id)):
            pass
    except ValueError as exc:
        raise EvaluationFailure('后台进程仍在执行或生成报告，请稍后再操作。', 409) from exc
    store.queue(job_id)
    directory = store.job_directory(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-B', '-m', 'src.evaluation.platform_worker', '--root', str(store.root), '--job', job_id]
    if retry_failed:
        command.append('--retry-failed')
    kwargs = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL, close_fds=True)
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs['start_new_session'] = True
    try:
        with (directory / 'worker.log').open('ab') as log:
            subprocess.Popen(command, stdout=log, stderr=log, **kwargs)
    except OSError as exc:
        store.state(job_id, 'error', '后台进程启动失败，请检查 Python 环境。')
        raise EvaluationFailure('后台进程启动失败。', 503) from exc
    return store.job(job_id)


def stop_job(store, job_id):
    if not store.transition(job_id, 'stopping', ('running', 'queued', 'stopping')):
        raise EvaluationFailure('任务当前未在运行。', 409)
    return store.job(job_id)


def collect(store, job_id, frozen, ledger, repository, *, settle=False):
    records = {r['_id']: r for r in frozen['records']}
    rows = []
    for attempt in ledger.attempts():
        detail = repository.get_run_detail(attempt['run_id'])
        if detail and (detail.run.context.experiment_id != job_id or detail.run.context.sample_id != attempt['sample_id']
                       or detail.run.context.method_id != attempt['method_id']):
            raise EvaluationFailure('运行身份与任务账本不一致。')
        if not detail and attempt['status'] in ('completed', 'failed', 'cancelled'):
            raise EvaluationFailure('已结束任务的运行记录缺失，拒绝重放。')
        if detail and detail.run.status in ('completed', 'failed', 'cancelled'):
            result = result_for(detail, records[attempt['sample_id']], frozen['request']['context_mode'] == 'question_context')
            if settle and attempt['status'] == 'running':
                ledger.finish(attempt['run_id'], detail.run.status, result)
                attempt['status'] = detail.run.status
        else:
            result = dict(sample_id=attempt['sample_id'], method_id=attempt['method_id'], run_id=attempt['run_id'],
                          status='interrupted', answer='', scores=None, metrics={}, error=None,
                          question=records[attempt['sample_id']]['question'], gold=records[attempt['sample_id']]['answer'],
                          labels=['interrupted_unknown'])
            if settle and attempt['status'] == 'running':
                ledger.finish(attempt['run_id'], 'interrupted', result)
                attempt['status'] = 'interrupted'
        rows.append({**result, 'attempt': attempt['attempt'], 'repeat': attempt['repeat'], 'task_id': attempt['task_id']})
    return rows


def aggregate(rows, planned, labelled_planned):
    statuses = {key: sum(r['status'] == key for r in rows) for key in ('completed', 'failed', 'interrupted', 'cancelled')}
    complete = len(rows) == planned and statuses['completed'] + statuses['failed'] == planned
    result = dict(planned=planned, attempted=len(rows), statuses=statuses, complete=complete,
                  labelled_planned=labelled_planned, scores={}, usage={},
                  labels=dict(Counter(label for r in rows for label in r.get('labels', []))))
    for name in ('em', 'f1', 'sp_em', 'sp_f1', 'joint_em', 'joint_f1'):
        values = [r['scores'][name] for r in rows if r.get('scores') is not None and name in r['scores']]
        # Support labels may cover fewer items than answer labels; disclose each denominator.
        result['scores'][name] = dict(value=sum(values) / len(values) if complete and values else None,
                                     scored=len(values), observed_mean=sum(values) / len(values) if values else None)
    for name in ('total_tokens', 'llm_call_count', 'duration_ms'):
        values = [r.get('metrics', {}).get(name) for r in rows]
        known = [v for v in values if v is not None]
        result['usage'][name] = dict(reported_total=sum(known) if known else None, reported=len(known), attempted=len(rows),
                                   mean_reported=sum(known) / len(known) if known else None, p95_reported=percentile(known, .95))
    return result


def report(store, job_id, frozen, ledger, repository):
    rows = collect(store, job_id, frozen, ledger, repository)
    primary = [r for r in rows if r['attempt'] == 1]
    latest = {r['task_id']: r for r in rows}
    planned = len(frozen['records']) * frozen['repeats']
    labelled = sum(r['answer'] is not None for r in frozen['records']) * frozen['repeats']
    methods = {m: aggregate([r for r in primary if r['method_id'] == m], planned, labelled) for m in frozen['definitions']}
    summary = dict(job_id=job_id, primary_policy='first_attempt', complete=all(m['complete'] for m in methods.values()),
                   methods=methods, total_attempts=len(rows), retry_count=sum(r['attempt'] > 1 for r in rows),
                   latest_attempt_supplement={m: aggregate([r for r in latest.values() if r['method_id'] == m], planned, labelled)
                                              for m in frozen['definitions']},
                   all_attempt_usage=aggregate(rows, len(rows), labelled)['usage'])
    directory = store.job_directory(job_id)
    atomic_json(directory / 'results.json', rows)
    atomic_json(directory / 'summary.json', summary)
    return summary


def execute(store, job_id, *, retry_failed=False, service_factory=None):
    """service_factory is only an offline-test seam, never exposed to HTTP or the worker CLI."""
    directory = store.job_directory(job_id)
    with experiment_lock(directory):
        row, frozen = store.frozen(job_id)
        if identity() != frozen['identity']:
            raise EvaluationFailure('执行环境与冻结任务不一致，请新建评测。')
        if service_factory is None:
            load_dotenv(ROOT / '.env')
            require_env(ENV_API_KEY)
            provider = digest(require_env(ENV_BASE_URL).rstrip('/'))
        else:
            provider = 'offline_test'
        provider_path = directory / 'execution.json'
        execution = dict(provider=provider, identity=frozen['identity'])
        if provider_path.exists():
            if json.loads(provider_path.read_text(encoding='utf-8')) != execution:
                raise EvaluationFailure('模型端点或执行环境已变化，请新建评测。')
        else:
            atomic_json(provider_path, execution)
        if row['status'] == 'stopping':
            store.state(job_id, 'paused')
            return
        if row['status'] != 'queued':
            raise EvaluationFailure('任务没有获得执行授权。', 409)
        ledger_path = directory / 'tasks.sqlite3'
        is_existing = ledger_path.exists()
        ledger = Ledger(directory, existing=is_existing)
        try:
            config = ledger_config(frozen)
            if not is_existing:
                ledger.initialize(frozen['records'], config)
                atomic_json(directory / 'frozen.json', frozen)
                archive_source(directory)
            ledger.validate(frozen['records'], config)
            if ledger.attempts() and not (directory / 'runs.sqlite3').is_file():
                raise EvaluationFailure('运行库缺失，拒绝重放任务。')
            repository = SQLiteRunRepository(directory / 'runs.sqlite3')
            service = (service_factory or RunService)(repository)
            collect(store, job_id, frozen, ledger, repository, settle=True)
            if not store.transition(job_id, 'running', ('queued',)):
                store.state(job_id, 'paused')
                return
            records = {r['_id']: r for r in frozen['records']}
            for task in ledger.tasks():
                if store.frozen(job_id)[0]['status'] == 'stopping':
                    store.state(job_id, 'paused')
                    break
                attempts = ledger.attempts(task['task_id'])
                if attempts and (not retry_failed or attempts[-1]['status'] not in ('failed', 'interrupted', 'cancelled')):
                    continue
                definition = frozen['definitions'][task['method_id']]
                record = records[task['sample_id']]
                run_id = uuid4().hex
                if frozen['request']['context_mode'] == 'question_context':
                    context = build_context(definition, record, directory, job_id, run_id, code_version())
                else:
                    snapshot = deepcopy(definition['snapshot'])
                    model = next(a['model_config']['model_name'] for a in snapshot['agents'].values() if a.get('model_config'))
                    context = RunContext(run_id=run_id, entrypoint='batch', question=record['question'],
                        workflow_id=snapshot['workflow_spec']['protocol_id'], protocol_id=snapshot['workflow_spec']['protocol_id'],
                        method_id=task['method_id'], model_name=model, code_version=code_version(), config_snapshot=snapshot,
                        experiment_id=job_id, sample_id=record['_id'])
                ledger.start(task, run_id)
                detail = service.run(context)
                atomic_json(directory / 'runs' / f'{run_id}.json', detail.model_dump(mode='json'))
                ledger.finish(run_id, detail.run.status, result_for(detail, record, frozen['request']['context_mode'] == 'question_context'))
            else:
                store.state(job_id, 'completed')
        finally:
            try:
                if 'repository' in locals():
                    report(store, job_id, frozen, ledger, repository)
            finally:
                ledger.close()


def exported_csv(rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['sample_id', 'method_id', 'repeat', 'attempt', 'run_id', 'status', 'question', 'answer', 'gold', 'scores', 'labels', 'error', 'human_label', 'human_notes'])
    for r in rows:
        values = [r.get(k, '') for k in ('sample_id', 'method_id', 'repeat', 'attempt', 'run_id', 'status', 'question', 'answer', 'gold', 'scores', 'labels', 'error')]
        # Spreadsheet-safe text, including researcher-provided questions and model output.
        writer.writerow([("'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else
                          json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for v in values] + ['', ''])
    return '\ufeff' + output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    store = EvaluationStore(args.root)
    try:
        execute(store, args.job, retry_failed=args.retry_failed)
    except Exception as exc:
        message = str(exc) if isinstance(exc, EvaluationFailure) else '后台执行异常；请检查运行记录和本地环境后恢复。'
        store.state(args.job, 'error', message)
        print(json.dumps(dict(error=type(exc).__name__, message=message), ensure_ascii=True), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
