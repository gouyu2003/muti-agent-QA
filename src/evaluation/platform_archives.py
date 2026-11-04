"""Import read-only CLI result snapshots; never initialize, repair or resume an archive."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing, contextmanager
from uuid import uuid4

from src.evaluation.experiment_config import ROOT, digest
from src.evaluation.experiment_store import utcnow
from src.evaluation.platform_store import EvaluationFailure
from src.evaluation.platform_reports import relative_pairs
from src.runtime.history import ReadOnlyArchive


def root():
    return Path(os.getenv('MAQA_EXPERIMENTS_DIR') or ROOT / 'data/experiments').resolve()


def directory(key):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', key) or key in ('.', '..'):
        raise EvaluationFailure('CLI 档案名称无效。')
    path = (root() / key).resolve()
    if path.parent != root() or not path.is_dir():
        raise EvaluationFailure('CLI 档案不在已配置的实验目录内。', 404)
    return path


def file_at(path, name):
    relative = Path(name.replace('\\', '/'))
    resolved = (path / relative).resolve()
    if relative.is_absolute() or not resolved.is_relative_to(path) or not resolved.is_file():
        raise EvaluationFailure('CLI 档案文件缺失或路径超出原目录。')
    return resolved


def header(path):
    files = {}
    def read(name):
        raw = file_at(path, name).read_bytes()
        files[name] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw.decode('utf-8-sig'))
    config = read('config.resolved.json')
    manifest = read('manifest.json')
    provenance = read('provenance.json')
    latest = read('reports/latest.json')
    report_name = latest['path'].replace('\\', '/')
    if not report_name.endswith('.json'): report_name += '/summary.json'
    summary = read(report_name)
    if digest(config) != provenance['config_sha256'] or digest(manifest) != provenance['manifest_sha256']:
        raise EvaluationFailure('CLI 冻结配置或数据清单指纹不一致。')
    if digest(summary) != latest['sha256'] or digest(summary['provenance']) != digest(provenance):
        raise EvaluationFailure('CLI 报告或来源指纹不一致。')
    if summary['experiment_id'] != path.name:
        raise EvaluationFailure('CLI 报告身份与目录不一致。')
    return dict(config=config, manifest=manifest, provenance=provenance, summary=summary, files=files)


def open_ledger(path):
    database = file_at(path, 'tasks.sqlite3')
    for name in ('tasks.sqlite3-wal', 'runs.sqlite3-wal'):
        wal = path / name
        if wal.exists() and wal.stat().st_size:
            raise EvaluationFailure('CLI 档案仍有未归档写入，请在原实验结束并生成报告后再导入。', 409)
    db = sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


@contextmanager
def archive_lock(path):
    lock = file_at(path, '.execution.lock')
    with lock.open('rb') as handle:
        acquired = False
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBRLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise EvaluationFailure('原 CLI 实验仍在运行或生成报告，请等待结束后导入。', 409) from exc
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == 'nt': msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def candidates():
    result = []
    if not root().is_dir(): return result
    for path in sorted(root().iterdir()):
        if not path.is_dir() or not (path / 'provenance.json').is_file(): continue
        reason = None
        data = None
        try:
            path = directory(path.name)
            data = header(path)
            with archive_lock(path), closing(open_ledger(path)) as db:
                if db.execute("SELECT 1 FROM attempts WHERE status='running' LIMIT 1").fetchone():
                    raise EvaluationFailure('CLI 尚有未核对终态的尝试，不能登记为稳定只读档案。')
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            reason = str(exc) if isinstance(exc, EvaluationFailure) else 'CLI 档案格式不完整或无法读取。'
        result.append(dict(key=path.name, name=path.name, available=reason is None, reason=reason,
                           purpose=data['config'].get('purpose') if data else None,
                           complete=data['summary'].get('complete') if data else None))
    return result


def normalize_method(data, scored):
    return dict(planned=data['planned'], attempted=data['attempted'], complete=data['complete'],
        statuses={s: data.get('status_counts', {}).get(s, 0) for s in ('completed', 'failed', 'interrupted', 'cancelled')},
        scores={metric: dict(value=value, scored=scored.get(metric, data['attempted'] if value is not None else 0), observed_mean=data.get('observed_scores', {}).get(metric))
                for metric, value in data['scores'].items()}, usage=data['metrics'])


def job_view(key, data, rows, fingerprint):
    config, report = data['config'], data['summary']
    first = [r for r in rows if r['attempt'] == 1]
    scored = {m: Counter(metric for r in first if r['method_id'] == m for metric in (r.get('scores') or {})) for m in config['methods']}
    methods = {m: normalize_method(report['methods'][m], scored[m]) for m in config['methods']}
    reference = 'single_rag' if 'single_rag' in methods else next(iter(methods))
    research_methods = {m: dict(by_type={k: normalize_method(v, {}) for k, v in report['methods'][m].get('by_type', {}).items()},
        by_repeat={str(int(k) + 1): normalize_method(v, {}) for k, v in report['methods'][m].get('by_repeat', {}).items()},
        repeat_score_std=report['methods'][m].get('repeat_score_std', {})) for m in methods}
    counts = Counter(r['status'] for r in first)
    return dict(job_id=key, name=f"CLI · {report['experiment_id']}", readonly=True, archived=True,
        status='completed' if report['complete'] else 'interrupted', error=None,
        sample_count=len(data['manifest']['samples']), repeats=config['repeats'], fingerprint=fingerprint,
        bank=dict(bank_id='cli_snapshot', version=1, name=report['experiment_id'], sha256=data['provenance']['manifest_sha256']),
        context_mode='question_context', targets=[dict(method_id=m, label=m) for m in methods], retryable_count=0,
        progress=dict(attempted=len(first), planned=len(data['manifest']['samples']) * config['repeats'] * len(methods),
                      **{s: counts[s] for s in ('completed', 'failed', 'interrupted', 'cancelled')}),
        report=dict(complete=report['complete'], methods=methods, retry_count=report.get('retry_count', 0),
            latest_attempt_supplement={m: normalize_method(v, {}) for m, v in report.get('latest_attempt_supplement', {}).items()},
            all_attempt_usage=report.get('all_attempt_costs', {}), research=dict(complete=report['complete'],
                primary_metric='f1', reference_method=reference, methods=research_methods, pairs=report['pairs'],
                reference_pairs=relative_pairs(report['pairs'], reference),
                bootstrap=dict(seed=report['bootstrap']['seed'], replicates=report['bootstrap']['replicates'],
                               unit=report['bootstrap']['unit'], confidence=0.95), notes=report.get('limitations', []))))


def import_archive(store, key):
    path = directory(key)
    try:
        data = header(path)
        config = data['config']
        samples = {r['sample_id'] for r in data['manifest']['samples']}
        expected = {(sample, method, repeat) for sample in samples for method in config['methods'] for repeat in range(config['repeats'])}
        rows = []
        with archive_lock(path), closing(open_ledger(path)) as db:
            tasks = {(r['sample_id'], r['method_id'], r['repeat']) for r in db.execute('SELECT * FROM tasks')}
            if tasks != expected:
                raise EvaluationFailure('CLI 任务账本与原冻结清单不一致。')
            for row in db.execute('SELECT a.*,t.sample_id,t.method_id,t.repeat FROM attempts a JOIN tasks t USING(task_id) ORDER BY task_id,attempt'):
                if row['status'] == 'running' or row['result_json'] is None:
                    raise EvaluationFailure('CLI 尚有状态不明或未生成结果的尝试，不能导入。')
                result = json.loads(row['result_json'])
                if (result['sample_id'], result['method_id'], result['run_id'], result['status']) != (
                        row['sample_id'], row['method_id'], row['run_id'], row['status']):
                    raise EvaluationFailure('CLI 逐题结果身份不一致。')
                rows.append({**result, 'attempt': row['attempt'], 'repeat': row['repeat']})
        for method, report in data['summary']['methods'].items():
            first = [r for r in rows if r['method_id'] == method and r['attempt'] == 1]
            if len(first) != report['attempted'] or Counter(r['status'] for r in first) != Counter(report['status_counts']):
                raise EvaluationFailure('CLI 报告落后于任务账本，请使用原实验最后一次报告。')
        if data['files'] != header(path)['files']:
            raise EvaluationFailure('CLI 档案在导入期间发生变化。', 409)
    except EvaluationFailure:
        raise
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise EvaluationFailure('CLI 档案格式不兼容或不可读。') from exc
    fingerprint = digest([data['files'], [digest(r) for r in rows]])
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute("SELECT id FROM cli_archives WHERE json_extract(document,'$.fingerprint')=?", (fingerprint,)).fetchone()
        if existing: return get_archive(store, existing[0])
        archive_id = 'archive_' + uuid4().hex
        document = dict(archive_id=archive_id, source_key=key, fingerprint=fingerprint, imported_at=utcnow(),
            source_files=data['files'], original_report=data['summary'], config=config,
            manifest=data['manifest'], provenance=data['provenance'], view=job_view(archive_id, data, rows, fingerprint))
        document['sha256'] = digest(document)
        db.execute('INSERT INTO cli_archives VALUES (?,?,?)', (archive_id, json.dumps(document, ensure_ascii=False), document['imported_at']))
        db.executemany('INSERT INTO cli_results VALUES (?,?,?)', [(archive_id, i, json.dumps(row, ensure_ascii=False)) for i, row in enumerate(rows)])
    return document


def get_archive(store, key):
    with store.connection() as db:
        row = db.execute('SELECT document FROM cli_archives WHERE id=?', (key,)).fetchone()
    if not row: raise EvaluationFailure('只读 CLI 档案不存在。', 404)
    document = json.loads(row[0])
    if digest({k: v for k, v in document.items() if k != 'sha256'}) != document['sha256']:
        raise EvaluationFailure('导入档案快照指纹不一致。')
    return document


def archive_results(store, key, offset=0, limit=None, *, view='all', status=None, method=None, label=None, search=None, sample_id=None, repeat=None):
    get_archive(store, key)
    clauses = ['a.archive_id=?']; params = [key]
    if view == 'first': clauses.append("json_extract(a.document,'$.attempt')=1")
    elif view == 'latest':
        clauses.append('''json_extract(a.document,'$.attempt')=(SELECT MAX(json_extract(b.document,'$.attempt'))
            FROM cli_results b WHERE b.archive_id=a.archive_id
            AND json_extract(b.document,'$.sample_id')=json_extract(a.document,'$.sample_id')
            AND json_extract(b.document,'$.method_id')=json_extract(a.document,'$.method_id')
            AND json_extract(b.document,'$.repeat')=json_extract(a.document,'$.repeat'))''')
    elif view != 'all': raise EvaluationFailure('未知尝试筛选。')
    for field, value in [('method_id', method), ('sample_id', sample_id), ('repeat', repeat)]:
        if value is not None:
            clauses.append(f"json_extract(a.document,'$.{field}')=?"); params.append(value)
    with store.connection() as db:
        labels = [r[0] for r in db.execute("SELECT DISTINCT l.value FROM cli_results a,json_each(json_extract(a.document,'$.labels')) l WHERE " + ' AND '.join(clauses) + ' ORDER BY l.value', params)]
        if status:
            clauses.append("json_extract(a.document,'$.status')=?"); params.append(status)
        if label:
            clauses.append("EXISTS(SELECT 1 FROM json_each(json_extract(a.document,'$.labels')) l WHERE l.value=?)"); params.append(label)
        if search:
            value = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            clauses.append("(json_extract(a.document,'$.sample_id')=? OR json_extract(a.document,'$.question') LIKE ? ESCAPE '\\' OR json_extract(a.document,'$.answer') LIKE ? ESCAPE '\\')")
            params += [search, value, value]
        query = ' FROM cli_results a WHERE ' + ' AND '.join(clauses)
        total = db.execute('SELECT COUNT(*)' + query, params).fetchone()[0]
        rows = db.execute('SELECT a.document' + query + ' ORDER BY position' + (' LIMIT ? OFFSET ?' if limit is not None else ''),
                          params + ([limit, offset] if limit is not None else [])).fetchall()
    return dict(items=[json.loads(r[0]) for r in rows], total=total, labels=labels)


def archive_detail(store, key, run_id):
    archive = get_archive(store, key)
    with store.connection() as db:
        row = db.execute("SELECT document FROM cli_results WHERE archive_id=? AND json_extract(document,'$.run_id')=? LIMIT 1", (key, run_id)).fetchone()
    if not row: raise EvaluationFailure('该运行不属于所选 CLI 档案。', 404)
    path = directory(archive['source_key'])
    try:
        unchanged = header(path)['files'] == archive['source_files']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EvaluationFailure('原 CLI 档案无法核验；导入快照保留，原轨迹暂不可读取。', 409) from exc
    if not unchanged:
        raise EvaluationFailure('原 CLI 档案已变化；导入快照仍保留，原轨迹暂不可读取。', 409)
    detail = ReadOnlyArchive(file_at(path, 'runs.sqlite3')).get_run_detail(run_id)
    result = json.loads(row[0])
    if not detail or (detail.run.context.experiment_id, detail.run.context.sample_id, detail.run.context.method_id) != (
            archive['source_key'], result['sample_id'], result['method_id']):
        raise EvaluationFailure('原 CLI 轨迹身份不一致。')
    return detail
