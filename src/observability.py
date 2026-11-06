"""Dependency-free public observations; no prompts or raw model replies are stored."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import sqlite3
from threading import RLock
from time import perf_counter
from uuid import uuid4
from contextlib import closing

logger = logging.getLogger(__name__)
current_observer = ContextVar('run_observer', default=None)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class RunObserver:
    def __init__(self, run_id, database=None, *, clock=perf_counter, identifiers=None):
        self.run_id, self.database, self.clock = run_id, database, clock
        self.identifiers = {'run_id': run_id, **(identifiers or {})}
        self.started = clock()
        self.records, self.lock = [], RLock()
        if self.database:
            with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS run_observations '
                           '(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,document TEXT NOT NULL)')
                db.execute('CREATE INDEX IF NOT EXISTS idx_observations_run ON run_observations(run_id)')

    def emit(self, kind, **payload):
        record = {'id': uuid4().hex, 'kind': kind, 'timestamp': utcnow(),
                  **self.identifiers, **payload}
        with self.lock:
            self.records.append(record)
            if self.database:
                with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
                    db.execute('INSERT INTO run_observations VALUES (?,?,?)',
                               (record['id'], self.run_id, json.dumps(record, ensure_ascii=False)))
        # Only application-authored labels and correlation IDs enter the log.
        logger.info('run=%s stage=%s agent=%s call=%s request=%s kind=%s name=%s outcome=%s',
                    self.run_id, payload.get('stage_id'), payload.get('agent_id'),
                    payload.get('call_id'), payload.get('request_call_id'), kind,
                    payload.get('name'), payload.get('outcome'))
        return record

    @contextmanager
    def span(self, name, **metadata):
        span_id = uuid4().hex
        start = self.clock()
        offset = start * 1000  # System monotonic origin is shared across local worker processes.
        self.emit('activity_started', span_id=span_id, name=name, **metadata)
        outcome = 'completed'
        try:
            yield span_id
        except BaseException:
            outcome = 'failed'
            raise
        finally:
            self.emit('activity_finished', span_id=span_id, name=name, outcome=outcome,
                      offset_ms=round(offset), duration_ms=max(0, round((self.clock()-start)*1000)), **metadata)

    def timings(self):
        return summarize_timings(self.records)


@contextmanager
def observe(name, **metadata):
    observer = current_observer.get()
    if observer is None:
        yield None
    else:
        with observer.span(name, **metadata) as span_id:
            yield span_id


def read_observations(database, run_id, *, immutable=False):
    # immutable avoids WAL/SHM side effects for frozen archives; active WAL stores need a live read.
    uri = database.resolve().as_uri() + '?mode=ro'
    if immutable or not database.with_name(database.name + '-wal').exists():
        uri += '&immutable=1'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        try:
            return [json.loads(r[0]) for r in db.execute(
                'SELECT document FROM run_observations WHERE run_id=? ORDER BY rowid', (run_id,))]
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return []
            raise


def summarize_timings(records):
    spans = [r for r in records if r['kind'] == 'activity_finished']
    categories = {}
    for name in sorted({r['name'] for r in spans}):
        own = [r for r in spans if r['name'] == name]
        intervals = sorted((r['offset_ms'], r['offset_ms'] + r['duration_ms']) for r in own)
        union = 0
        end = 0
        for left, right in intervals:
            union += max(0, right-max(left, end))
            end = max(end, right)
        categories[name] = {'wall_ms': union, 'cumulative_ms': sum(r['duration_ms'] for r in own), 'count': len(own)}
    task_total = max(r['offset_ms']+r['duration_ms'] for r in spans)-min(r['offset_ms'] for r in spans) if spans else None
    return {'categories': categories, 'spans': spans, 'task_total_wall_ms': task_total,
            'cache': [r for r in records if r['kind'] == 'cache_lookup'],
            'note': '同类区间合并计算经过时间；不同类别可能嵌套，不可相加。'} if spans else {}


def activity_snapshot(records):
    active = {}
    for record in records:
        if record['kind'] == 'activity_started':
            active[record['span_id']] = record
        elif record['kind'] == 'activity_finished':
            active.pop(record['span_id'], None)
    return list(active.values())


def diagnostic_codes(agent_usage):
    result = set()
    for usage in (agent_usage or {}).values():
        for call in usage.get('calls', []):
            if call.get('outcome') != 'ok':
                result.add(call.get('outcome', 'unknown'))
            result.update(issue['code'] for issue in call.get('validation_errors', []))
            if call.get('request_error', {}).get('category'):
                result.add('provider_' + call['request_error']['category'])
    return sorted(result)
