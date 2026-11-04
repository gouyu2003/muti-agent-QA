"""Read-only progress and result review, with explicit first/latest attempt policies."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import sqlite3

from src.evaluation.platform_store import EvaluationFailure


@contextmanager
def readonly(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        yield db


def result_rows(catalog, job_id, offset=0, limit=None, *, view='all', status=None, method=None, label=None, search=None, sample_id=None, repeat=None):
    path = catalog.job_directory(job_id) / 'tasks.sqlite3'
    if not path.is_file():
        return dict(items=[], total=0, labels=[])
    clauses = ['a.result_json IS NOT NULL']
    params = []
    if view == 'first':
        clauses.append('a.attempt=1')
    elif view == 'latest':
        clauses.append('a.attempt=(SELECT MAX(b.attempt) FROM attempts b WHERE b.task_id=a.task_id)')
    elif view != 'all':
        raise EvaluationFailure('未知的尝试筛选方式。')
    if method:
        clauses.append('t.method_id=?'); params.append(method)
    if sample_id is not None:
        clauses.append('t.sample_id=?'); params.append(sample_id)
    if repeat is not None:
        clauses.append('t.repeat=?'); params.append(repeat)
    with readonly(path) as db:
        try:
            labels = [r[0] for r in db.execute("SELECT DISTINCT l.value FROM attempts a JOIN tasks t USING(task_id), json_each(json_extract(a.result_json,'$.labels')) l WHERE " + ' AND '.join(clauses) + ' ORDER BY l.value', params)]
            if status:
                clauses.append('a.status=?'); params.append(status)
            if label:
                clauses.append("EXISTS(SELECT 1 FROM json_each(json_extract(a.result_json,'$.labels')) l WHERE l.value=?)")
                params.append(label)
            if search:
                escaped = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                clauses.append("(t.sample_id=? OR json_extract(a.result_json,'$.question') LIKE ? ESCAPE '\\' OR json_extract(a.result_json,'$.answer') LIKE ? ESCAPE '\\')")
                params += [search, '%' + escaped + '%', '%' + escaped + '%']
            query = ' FROM attempts a JOIN tasks t USING(task_id) WHERE ' + ' AND '.join(clauses)
            total = db.execute('SELECT COUNT(*)' + query, params).fetchone()[0]
            rows = db.execute('SELECT a.result_json,a.attempt,t.repeat' + query + ' ORDER BY t.task_id,a.attempt' +
                              (' LIMIT ? OFFSET ?' if limit is not None else ''),
                              params + ([limit, offset] if limit is not None else [])).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return dict(items=[], total=0, labels=[])
            raise
    return dict(items=[{**json.loads(r[0]), 'attempt': r[1], 'repeat': r[2]} for r in rows], total=total,
                labels=[v for v in labels if isinstance(v, str)])


def compare_question(catalog, job_id, sample_id, repeat, view):
    job, frozen = catalog.frozen(job_id)
    record = next((r for r in frozen['records'] if r['_id'] == sample_id), None)
    if record is None or not 0 <= repeat < frozen['repeats']:
        raise EvaluationFailure('题目或重复轮次不存在。', 404)
    selected = result_rows(catalog, job_id, view=view, sample_id=sample_id, repeat=repeat)['items']
    path = catalog.job_directory(job_id) / 'tasks.sqlite3'
    if path.is_file():
        policy = ' AND a.attempt=1' if view == 'first' else (
            ' AND a.attempt=(SELECT MAX(b.attempt) FROM attempts b WHERE b.task_id=a.task_id)' if view == 'latest' else '')
        with readonly(path) as db:
            pending = db.execute('''SELECT a.run_id,a.attempt,a.status,t.method_id FROM attempts a JOIN tasks t USING(task_id)
                WHERE t.sample_id=? AND t.repeat=? AND a.result_json IS NULL''' + policy, (sample_id, repeat)).fetchall()
        for attempt in pending:
            status = attempt['status']
            if status == 'running' and job['status'] not in ('queued', 'running', 'stopping'): status = 'interrupted'
            selected.append(dict(sample_id=sample_id, method_id=attempt['method_id'], repeat=repeat, attempt=attempt['attempt'],
                                 run_id=attempt['run_id'], status=status, question=record['question'], answer='', scores=None, labels=[], error=None))
    for method in frozen['definitions']:
        if not any(r['method_id'] == method for r in selected):
            selected.append(dict(sample_id=sample_id, method_id=method, repeat=repeat, attempt=0, run_id=None,
                                 status='pending', question=record['question'], answer='', scores=None, labels=[], error=None))
    return dict(question=record['question'], gold=record['answer'], sample_id=sample_id, repeat=repeat, items=selected)


def seconds_since(value):
    return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()) if value else None


def monitor(catalog, job_id):
    job = catalog.job(job_id)
    path = catalog.job_directory(job_id) / 'tasks.sqlite3'
    empty = dict(current=None, methods=[], last_event_at=None, completed_attempt_seconds=0,
                 estimated_remaining_seconds=None, failure_labels={}, sampled_at=datetime.now(timezone.utc).isoformat())
    if not path.is_file():
        return empty
    with readonly(path) as db:
        try:
            counts = db.execute('''SELECT t.method_id,COUNT(*) planned,
                SUM(a.status='completed') completed,SUM(a.status='failed') failed,
                SUM(a.status IN ('interrupted','cancelled') OR (a.status='running' AND ?)) unresolved,
                SUM(a.status='running' AND ?) in_progress,
                SUM(a.status IS NULL) unstarted
                FROM tasks t LEFT JOIN attempts a ON a.task_id=t.task_id AND a.attempt=1 GROUP BY t.method_id''',
                (job['status'] not in ('queued', 'running', 'stopping'), job['status'] in ('queued', 'running', 'stopping'))).fetchall()
            current = db.execute('''SELECT a.run_id,a.started_at,t.sample_id,t.method_id,t.repeat,a.attempt
                FROM attempts a JOIN tasks t USING(task_id) WHERE a.status='running' ORDER BY a.started_at DESC LIMIT 1''').fetchone()
            durations = db.execute('''SELECT SUM(json_extract(result_json,'$.metrics.duration_ms')) total,
                AVG(CASE WHEN attempt=1 THEN json_extract(result_json,'$.metrics.duration_ms') END) initial_mean
                FROM attempts WHERE result_json IS NOT NULL''').fetchone()
            labels = db.execute('''SELECT l.value,COUNT(*) FROM attempts a,
                json_each(json_extract(a.result_json,'$.labels')) l
                WHERE a.status!='completed' GROUP BY l.value''').fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc): return empty
            raise
    remaining = job['progress']['planned'] - job['progress']['attempted']
    empty.update(methods=[{k: (r[k] or 0) for k in r.keys()} for r in counts],
        completed_attempt_seconds=(durations['total'] or 0) / 1000,
        failure_labels={r[0]: r[1] for r in labels if isinstance(r[0], str)},
        estimated_remaining_seconds=durations['initial_mean'] / 1000 * remaining if durations['initial_mean'] is not None else None)
    runtime = catalog.job_directory(job_id) / 'runs.sqlite3'
    if current and job['status'] in ('queued', 'running', 'stopping') and runtime.is_file():
        with readonly(runtime) as db:
            run = db.execute('SELECT question,status FROM runs WHERE run_id=?', (current['run_id'],)).fetchone()
            events = db.execute('''SELECT event_type,timestamp,node,cycle,
                json_extract(payload_json,'$.call_id') call_id,json_extract(payload_json,'$.stage_id') stage_id,
                json_extract(payload_json,'$.agent_name') agent_name
                FROM trace_events WHERE run_id=? ORDER BY sequence''', (current['run_id'],)).fetchall()
        active = {}
        for event in events:
            key = event['call_id'] or (event['node'], event['cycle'])
            if event['event_type'] == 'node_started': active[key] = dict(event)
            elif event['event_type'] == 'node_completed': active.pop(key, None)
        empty['current'] = {**dict(current), 'question': run['question'] if run else '',
                            'status': run['status'] if run else 'running', 'phases': list(active.values()) if run and run['status'] == 'running' else [],
                            'elapsed_seconds': seconds_since(current['started_at'])}
        empty['last_event_at'] = events[-1]['timestamp'] if events else current['started_at']
    return empty
