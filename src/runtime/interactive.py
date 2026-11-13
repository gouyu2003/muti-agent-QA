"""Single-user detached QA execution and durable, idempotent control."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from time import sleep
from uuid import uuid4

from src.evaluation.experiment_store import experiment_lock
from src.runtime.models import RunError, RunMetrics, RunRecord, TraceEvent
from src.observability import activity_snapshot, read_observations, summarize_timings, utcnow
from src.runtime.repository import InvalidRunTransitionError
from src.state import merge_state

ROOT = Path(__file__).resolve().parents[2]


class ExecutionFailure(ValueError):
    def __init__(self, message, status=409, active_run_id=None):
        super().__init__(message)
        self.status = status
        self.active_run_id = active_run_id


def seconds_since(timestamp):
    return max(0, (datetime.now(timezone.utc)-datetime.fromisoformat(timestamp)).total_seconds())


class InteractiveRuns:
    def __init__(self, repository, *, launcher=None):
        self.repository, self.launcher = repository, launcher
        self.root = repository.db_path.parent / (repository.db_path.stem + '-interactive')
        self.root.mkdir(parents=True, exist_ok=True)
        with repository._connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS interactive_runs (
                run_id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,request_hash TEXT NOT NULL,
                phase TEXT NOT NULL,token TEXT NOT NULL,created_at TEXT NOT NULL,
                heartbeat_at TEXT,cancel_at TEXT)''')

    def row(self, run_id):
        with self.repository._connection() as db:
            row = db.execute('SELECT * FROM interactive_runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise ExecutionFailure('该运行不是可控制的实时问答任务。', 404)
        return dict(row)

    def existing(self, request_key, request_hash):
        with self.repository._connection() as db:
            row = db.execute('SELECT run_id,request_hash FROM interactive_runs WHERE request_key=?', (request_key,)).fetchone()
        if row:
            if row['request_hash'] != request_hash:
                raise ExecutionFailure('同一提交编号对应的问题或配置已改变，请重新提交。')
            return self.status(row['run_id'])

    def submit(self, context, request_key, request_hash, *, prepare=None, on_reserved=None):
        try:
            with experiment_lock(self.root):
                existing = self.existing(request_key, request_hash)
                if existing:
                    return existing
                self.reconcile_all()
                with self.repository._connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    active = db.execute("SELECT r.run_id FROM interactive_runs i JOIN runs r USING(run_id) WHERE r.status='running'").fetchone()
                    if active:
                        raise ExecutionFailure('已有实时问答在运行，请查看运行历史或停止后再提交。', active_run_id=active['run_id'])
                    if prepare:
                        context = prepare(context)
                    self.repository.create_run(RunRecord(context=context, status='running', started_at=datetime.now(timezone.utc)))
                    db.execute('INSERT INTO interactive_runs VALUES (?,?,?,?,?,?,NULL,NULL)',
                               (context.run_id, request_key, request_hash, 'queued', uuid4().hex, utcnow()))
                    if on_reserved:
                        on_reserved(context)
                try:
                    if self.launcher:
                        self.launcher(self, context)
                    else:
                        directory = self.root / context.run_id
                        directory.mkdir(exist_ok=True)
                        kwargs = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL, close_fds=True)
                        if os.name == 'nt':
                            # DETACHED_PROCESS overrides CREATE_NO_WINDOW and can leave
                            # the venv launcher with no console for its child to inherit.
                            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                        else:
                            kwargs['start_new_session'] = True
                        with (directory / 'worker.log').open('ab') as log:
                            subprocess.Popen([sys.executable, '-B', '-m', 'src.interactive_bootstrap',
                                              '--database', str(self.repository.db_path.resolve()), '--run', context.run_id,
                                              '--token', self.row(context.run_id)['token']], stdout=log, stderr=log, **kwargs)
                except OSError as exc:
                    self.settle(context.run_id, 'failed', 'worker_start_failed', '后台进程启动失败，请检查 Python 环境。')
                    raise ExecutionFailure('后台进程启动失败。', 503) from exc
                return self.status(context.run_id)
        except ValueError as exc:
            if isinstance(exc, ExecutionFailure):
                raise
            raise ExecutionFailure('另一项问答操作正在提交，请稍后重试。') from exc

    def settle(self, run_id, status, code, message):
        return settle_run(self.repository, run_id, status, code, message)

    def reconcile(self, run_id):
        row = self.row(run_id)
        run = self.repository.get_run(run_id)
        if run.status != 'running':
            return
        if row['phase'] == 'queued' and seconds_since(row['created_at']) < 30:
            return
        try:
            with experiment_lock(self.root / run_id):
                self.settle(run_id, 'cancelled' if row['cancel_at'] else 'interrupted',
                            'execution_interrupted', '执行进程已退出；已保存结果保留，未确认请求不会自动重发。')
        except ValueError:
            pass

    def reconcile_all(self):
        with self.repository._connection() as db:
            ids = [r[0] for r in db.execute("SELECT i.run_id FROM interactive_runs i JOIN runs r USING(run_id) WHERE r.status='running'")]
        for run_id in ids:
            self.reconcile(run_id)

    def cancel(self, run_id):
        row = self.row(run_id)
        if self.repository.get_run(run_id).status == 'running':
            with self.repository._connection() as db:
                db.execute("UPDATE interactive_runs SET cancel_at=COALESCE(cancel_at,?),phase='cancelling' WHERE run_id=?", (utcnow(), run_id))
        return self.status(run_id)

    def status(self, run_id):
        self.reconcile(run_id)
        row = self.row(run_id)
        run = self.repository.get_run(run_id)
        records = read_observations(self.repository.db_path, run_id)
        active = activity_snapshot(records) if run.status == 'running' else []
        events = self.repository.get_events(run_id)
        return {'run_id': run_id, 'status': run.status if run.status != 'running' else row['phase'],
                'heartbeat_at': row['heartbeat_at'], 'connection_independent': True,
                'heartbeat_stale': bool(row['heartbeat_at'] and seconds_since(row['heartbeat_at']) > 15),
                'cancel_requested': bool(row['cancel_at']), 'activities': active,
                'diagnostic_calls': [r for r in records if r['kind'] == 'model_call'],
                'last_progress_at': records[-1]['timestamp'] if records else events[-1].timestamp.isoformat() if events else row['created_at'],
                'events_url': f'/api/v1/runs/{run_id}/events/stream', 'timings': summarize_timings(records)}

    def subscribe(self, run_id, after=-1, *, legacy=False):
        from backend.streaming import encode_trace_event
        self.row(run_id)
        while True:
            for event in self.repository.get_events(run_id):
                if event.sequence > after:
                    yield ('' if legacy else f'id: {event.sequence}\n') + encode_trace_event(event)
                    after = event.sequence
            status = self.status(run_id)
            if not legacy:
                yield 'event: run_activity\ndata: ' + json.dumps({'run_id': run_id, 'timestamp': utcnow(), 'payload': status}, ensure_ascii=False) + '\n\n'
            if self.repository.get_run(run_id).status != 'running':
                # A terminal commit can race with the first read; drain it before closing.
                for event in self.repository.get_events(run_id):
                    if event.sequence > after:
                        yield ('' if legacy else f'id: {event.sequence}\n') + encode_trace_event(event)
                return
            sleep(1)


def request_fingerprint(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def settle_run(repository, run_id, status, code, message):
    """Called only by the owner watchdog or after the execution lock is released."""
    detail = repository.get_run_detail(run_id)
    if detail is None or detail.run.status != 'running':
        return
    state = {}
    terminal = None
    for event in detail.events:
        if event.event_type == 'node_completed':
            merge_state(state, event.payload.get('output', {}))
        if event.event_type in ('run_completed', 'run_failed', 'run_cancelled'):
            terminal = event
    if terminal:
        # Repair the pre-upgrade event/state gap without inventing a second terminal event.
        state = terminal.payload.get('result', state)
        saved_status = {'run_completed': 'completed', 'run_failed': 'failed', 'run_cancelled': 'cancelled'}[terminal.event_type]
        repository.update_run(detail.run.model_copy(update={'status': saved_status, 'finished_at': terminal.timestamp,
            'final_state': state, 'error': RunError(error_type=code, message=message) if saved_status == 'failed' else None}))
        return
    records = read_observations(repository.db_path, run_id)
    calls_by_id = {r.get('request_call_id', r['id']): r for r in records if r['kind'] == 'model_call'}
    now = datetime.now(timezone.utc)
    for started in records:
        if started['kind'] == 'activity_started' and started.get('name') == 'model_request' and started['request_call_id'] not in calls_by_id:
            calls_by_id[started['request_call_id']] = {**started, 'outcome': 'request_unconfirmed',
                'duration_ms': max(0, round((now-datetime.fromisoformat(started['timestamp'])).total_seconds()*1000)),
                'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None}
    calls = list(calls_by_id.values())
    groups = {}
    for call in calls:
        group = groups.setdefault(call.get('agent_id') or 'runtime', {'call_count': 0, 'total_tokens': None, 'calls': []})
        group['calls'].append(call)
        group['call_count'] += 1
    def total(field):
        return sum(c[field] for c in calls) if calls and all(c.get(field) is not None for c in calls) else None
    metrics = RunMetrics(duration_ms=round((now-detail.run.started_at).total_seconds()*1000),
                         node_call_count=sum(e.event_type == 'node_started' for e in detail.events),
                         llm_call_count=len(calls) if records else None,
                         prompt_tokens=total('prompt_tokens'), completion_tokens=total('completion_tokens'), total_tokens=total('total_tokens'),
                         agent_usage=groups, timings=summarize_timings(records))
    event = TraceEvent(run_id=run_id, sequence=len(detail.events), timestamp=now,
        event_type='run_cancelled' if status == 'cancelled' else 'run_failed',
        payload={'code': code, 'message': message, 'status': status})
    record = detail.run.model_copy(update={'status': status, 'finished_at': now, 'final_state': state,
        'metrics': metrics, 'error': None if status == 'cancelled' else RunError(error_type=code, message=message)})
    try:
        repository.finalize_run(record, event)
    except InvalidRunTransitionError:
        pass  # A durable result won a race with cancellation/recovery.

