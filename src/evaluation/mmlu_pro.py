"""Fixed-choice MMLU-Pro jobs with a durable, per-generation trace."""
from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Thread
from time import perf_counter
from uuid import uuid4

from src.evaluation.experiment_config import ROOT, digest
from src.evaluation.gsm8k_platform import _lock
from src.evaluation.platform_store import EvaluationFailure, EvaluationStore
from src.llm import DEFAULT_GENERATION_MODEL

METHODS = {'single': (1, 1), 'reflection': (1, 2), 'majority': (6, 1), 'debate': (3, 2)}
MANIFESTS = {
    'smoke': ('smoke-5', 'validation', 'cdf15f53eb0e269114c02689d801e0a8777ed1419d72446f127988cbd5da6316'),
    'development': ('development-100', 'test', '8a4f1bdcfcc16d997c9d8b9338d7697872e89530d00e25abbac0918b2406f359'),
    'calibration-60': ('calibration-60', 'test', 'ad7aabe16925fd04c7e983562ac62639027249e38cb529bc4f478be957fce473'),
}
LETTERS = 'ABCDEFGHIJ'
SCORING_VERSION = 'mmlu-pro-final-choice-v2'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MODEL = dict(model=DEFAULT_GENERATION_MODEL, temperature=1, top_p=1, max_tokens=2048,
             seed_base=1234, extra_body={'enable_thinking': False})
FINAL = 'End with exactly one line in the format FINAL: <letter>, choosing a displayed option.'


def load_manifest(selection):
    name, split, expected = MANIFESTS[selection]
    root = Path(os.environ.get('MAQA_MMLU_PRO_MANIFEST_DIR') or ROOT / 'data/experiments/mmlu-pro')
    try:
        manifest = json.loads((root / f'{name}.json').read_text(encoding='utf-8'))
        recorded = manifest.pop('manifest_sha256')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EvaluationFailure(f'无法读取固定题单 {name}。请检查 MMLU-Pro 数据目录。', 409) from exc
    if recorded != expected or digest(manifest) != expected or manifest['source_split'] != split:
        raise EvaluationFailure(f'固定题单 {name} 的指纹不符。', 409)
    return {**manifest, 'manifest_sha256': recorded}


def manifests():
    items = []
    for key in MANIFESTS:
        try:
            doc = load_manifest(key)
        except EvaluationFailure as exc:
            items.append(dict(selection=key, error=str(exc)))
            continue
        items.append(dict(selection=key, count=doc['sample_count'], split=doc['source_split'],
                          revision=doc['dataset_revision'], sha256=doc['manifest_sha256'],
                          quotas=doc['sampling']['quotas'], seed=doc['sampling']['seed']))
    return items


@contextmanager
def connection(root):
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / 'jobs.sqlite3', timeout=15)) as db:
        db.row_factory = sqlite3.Row
        with db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL,
                    error TEXT, profile TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    job_id TEXT NOT NULL, qid INTEGER NOT NULL, method TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT,
                    PRIMARY KEY (job_id, qid, method)
                );
                CREATE TABLE IF NOT EXISTS calls (
                    job_id TEXT NOT NULL, qid INTEGER NOT NULL, method TEXT NOT NULL,
                    round INTEGER NOT NULL, agent INTEGER NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL,
                    response TEXT, error TEXT, duration_ms INTEGER,
                    PRIMARY KEY (job_id, qid, method, round, agent)
                );
            """)
            yield db


def jobs_root():
    return EvaluationStore().root / 'mmlu-pro'


def _job(db, job_id):
    row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise EvaluationFailure('MMLU-Pro 实验不存在。', 404)
    return row, json.loads(row['profile'])


def _lock_path(root, job_id):
    if not re.fullmatch(r'mmlu_[0-9a-f]{32}', job_id):
        raise EvaluationFailure('MMLU-Pro 实验不存在。', 404)
    return root / 'locks' / (job_id + '.lock')


def create_job(name, methods, selection, fake):
    if not name.strip() or not methods or set(methods) - METHODS.keys() or len(set(methods)) != len(methods):
        raise EvaluationFailure('请填写实验名称并选择有效且不重复的方法。')
    manifest = load_manifest(selection)
    methods = [method for method in METHODS if method in methods]
    job_id = 'mmlu_' + uuid4().hex
    profile = dict(manifest=manifest, selection=selection, methods=methods, fake=fake, model=MODEL,
                   scoring=SCORING_VERSION, code_sha256=CODE_SHA256)
    profile['profile_sha256'] = digest(profile)
    root = jobs_root()
    with connection(root) as db:
        db.execute('INSERT INTO jobs VALUES (?,?,?,NULL,?)', (job_id, name.strip(), 'draft', json.dumps(profile)))
        db.executemany('INSERT INTO tasks VALUES (?,?,?,?,NULL)', [
            (job_id, record['question_id'], method, 'pending')
            for method in methods for record in manifest['records']
        ])
    return view_job(job_id)


def parse_choice(response, options):
    if response['finish_reason'] != 'stop':
        return None
    lines = [line.strip() for line in response['content'].strip().splitlines()]
    # Unwrap whole-line Markdown bold before checking for competing final answers.
    lines = [line[2:-2].strip() if line.startswith('**') and line.endswith('**') else line for line in lines]
    if not lines or sum(bool(re.match(r'^FINAL\s*:', line, re.I)) for line in lines) != 1:
        return None
    match = re.fullmatch(r'FINAL:\s*([A-J])', lines[-1], re.I)
    answer = match[1].upper() if match else None
    return answer if answer and answer in LETTERS[:len(options)] else None


def score_round(responses, record):
    candidates = [parse_choice(response, record['options']) for response in responses]
    votes = Counter(choice for choice in candidates if choice is not None)
    best = max(votes.values(), default=0)
    winners = [choice for choice, count in votes.items() if count == best]
    answer = winners[0] if len(winners) == 1 else None
    return dict(prediction=answer, correct=int(answer == record['answer']), candidates=candidates,
                votes=dict(votes), tie=len(winners) > 1, parse_failure=not votes,
                truncated=sum(response['finish_reason'] == 'length' for response in responses))


def question_text(record):
    options = '\n'.join(f'{letter}. {text}' for letter, text in zip(LETTERS, record['options']))
    return f"{record['question']}\n\nOptions\n{options}"


def model_client():
    from openai import OpenAI
    key = os.getenv('GSM_REPRO_API_KEY') or os.getenv('DEEPSEEK_API_KEY')
    url = os.getenv('GSM_REPRO_BASE_URL') or os.getenv('DEEPSEEK_BASE_URL')
    if not key or not url:
        raise EvaluationFailure('请先在后端配置模型 API Key 和 Base URL。', 409)
    return OpenAI(api_key=key, base_url=url, timeout=120, max_retries=0)


def generate(client, request, task_index, round_index, agent):
    if client is None:
        # Deterministic fixtures include disagreement, ties, missing answers, and truncation.
        demo = (('A', 'B', 'B'), ('A', 'A', 'B')) if task_index % 2 == 0 else (('B', 'A', 'C'), ('B', 'B', 'C'))
        answer = demo[round_index][agent % 3]
        reason = 'length' if task_index % 5 == 2 and agent == 0 else 'stop'
        text = 'Offline fixture.' if task_index % 5 == 3 and agent == 0 else f'Offline fixture.\nFINAL: {answer}'
        return dict(content=text, finish_reason=reason, usage=None)
    response = client.chat.completions.create(**request)
    choice = response.choices[0]
    return dict(content=choice.message.content or '', finish_reason=choice.finish_reason,
                usage=response.usage.model_dump() if response.usage else None)


def run_task(db, job_id, record, method, model, client, task_index):
    qid = record['question_id']
    agents, rounds = METHODS[method]
    text = question_text(record)
    initial = f'Answer this multiple-choice question. Briefly explain your reasoning. {FINAL}\n\n{text}'
    contexts = [[{'role': 'user', 'content': initial}] for _ in range(agents)]
    previous = []
    scores = []
    for round_index in range(rounds):
        responses = []
        for agent, messages in enumerate(contexts):
            if round_index:
                if method == 'reflection':
                    prompt = f'Double-check your previous answer. Briefly explain your reasoning. {FINAL}'
                else:
                    peers = '\n\n'.join(f'Agent {i + 1}:\n{peer["content"]}'
                                         for i, peer in enumerate(previous) if i != agent)
                    prompt = f'Review the other agents’ prior solutions before answering.\n\n{peers}\n\n{text}\n\n{FINAL}'
                messages.append({'role': 'user', 'content': prompt})
            key = (job_id, qid, method, round_index, agent)
            seed = int(digest([model['seed_base'], qid, method, round_index, agent])[:8], 16) % 2147483647
            request = {**{k: v for k, v in model.items() if k != 'seed_base'}, 'messages': messages, 'seed': seed}
            db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,'started',NULL,NULL,NULL)", (*key, json.dumps(request)))
            db.commit()
            started = perf_counter()
            try:
                response = generate(client, request, task_index, round_index, agent)
            except Exception as exc:
                # Never replay an attempt whose provider outcome was not confirmed.
                error = type(exc).__name__
                db.execute("""UPDATE calls SET state='unknown', error=?, duration_ms=?
                              WHERE job_id=? AND qid=? AND method=? AND round=? AND agent=?""",
                           (error, round((perf_counter() - started) * 1000), *key))
                db.commit()
                raise EvaluationFailure(f'模型请求中止（{error}），请核对调用记录。', 409) from exc
            db.execute("""UPDATE calls SET state='completed', response=?, duration_ms=?
                          WHERE job_id=? AND qid=? AND method=? AND round=? AND agent=?""",
                       (json.dumps(response), round((perf_counter() - started) * 1000), *key))
            db.commit()
            messages.append({'role': 'assistant', 'content': response['content']})
            responses.append(response)
        scores.append(score_round(responses, record))
        previous = responses
    return dict(scores=scores, outputs=[response['content'] for response in previous])


def _worker(root, job_id, profile, client, guard):
    records = {record['question_id']: record for record in profile['manifest']['records']}
    try:
        with connection(root) as db:
            tasks = db.execute("SELECT qid,method FROM tasks WHERE job_id=? AND state='pending' ORDER BY rowid",
                               (job_id,)).fetchall()
            checkpoint = (profile['selection'] == 'development' and tasks and
                          tasks[0]['method'] == 'single' and profile['methods'][-1] != 'single')
            for index, task in enumerate(tasks):
                qid, method = task['qid'], task['method']
                db.execute('BEGIN IMMEDIATE')
                stopping = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()[0] == 'stopping'
                if stopping or (checkpoint and method != 'single'):
                    note = None if stopping else 'Single 已完成。核对结果后，可继续其余方法。'
                    db.execute("UPDATE jobs SET state='paused',error=? WHERE id=?", (note, job_id))
                    db.commit()
                    return
                db.execute("UPDATE tasks SET state='running' WHERE job_id=? AND qid=? AND method=?",
                           (job_id, qid, method))
                db.commit()
                started = perf_counter()
                result = run_task(db, job_id, records[qid], method, profile['model'], client, index)
                result['duration_ms'] = round((perf_counter() - started) * 1000)
                db.execute("UPDATE tasks SET state='completed',result=? WHERE job_id=? AND qid=? AND method=?",
                           (json.dumps(result), job_id, qid, method))
                db.commit()
            db.execute("UPDATE jobs SET state='completed',error=NULL WHERE id=?", (job_id,))
    except Exception as exc:
        with connection(root) as db:
            db.execute("UPDATE tasks SET state='interrupted' WHERE job_id=? AND state='running'", (job_id,))
            message = str(exc) if isinstance(exc, EvaluationFailure) else f'执行中断（{type(exc).__name__}）。'
            db.execute("UPDATE jobs SET state='interrupted',error=? WHERE id=?", (message, job_id))
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            guard.__exit__(None, None, None)


def start_job(job_id, allow_model_calls):
    root = jobs_root()
    guard = _lock(_lock_path(root, job_id))
    guard.__enter__()
    client = None
    try:
        with connection(root) as db:
            row, profile = _job(db, job_id)
            if row['state'] not in ('draft', 'paused'):
                raise EvaluationFailure('只能启动草稿或继续已暂停的实验。', 409)
            if profile['code_sha256'] != CODE_SHA256:
                raise EvaluationFailure('执行代码与冻结草稿不同，请创建新实验。', 409)
            if not profile['fake'] and not allow_model_calls:
                raise EvaluationFailure('请确认真实模型调用。')
            client = None if profile['fake'] else model_client()
            if client is not None:
                endpoint = digest(str(client.base_url).rstrip('/'))
                if profile.get('endpoint_sha256', endpoint) != endpoint:
                    raise EvaluationFailure('模型端点与首次执行时不同，请检查配置。', 409)
                profile['endpoint_sha256'] = endpoint
                profile['profile_sha256'] = digest({key: value for key, value in profile.items()
                                                    if key != 'profile_sha256'})
            db.execute("UPDATE jobs SET state='running',error=NULL,profile=? WHERE id=?",
                       (json.dumps(profile), job_id))
        thread = Thread(target=_worker, args=(root, job_id, profile, client, guard), daemon=True)
        try:
            thread.start()
        except RuntimeError:
            with connection(root) as db:
                db.execute("UPDATE jobs SET state='paused',error=? WHERE id=?", ('后台线程启动失败。', job_id))
            raise
    except BaseException:
        try:
            if client is not None:
                client.close()
        finally:
            guard.__exit__(None, None, None)
        raise
    return view_job(job_id)


def stop_job(job_id):
    root = jobs_root()
    with connection(root) as db:
        changed = db.execute("UPDATE jobs SET state='stopping' WHERE id=? AND state='running'", (job_id,))
        if not changed.rowcount:
            raise EvaluationFailure('实验当前未运行。', 409)
    return view_job(job_id)


def _summary(db, row, profile):
    manifest = profile['manifest']
    job_id = row['id']
    counts = {state: 0 for state in ('pending', 'running', 'completed', 'failed', 'interrupted')}
    counts.update(dict(db.execute('SELECT state,count(*) FROM tasks WHERE job_id=? GROUP BY state', (job_id,))))
    using = db.execute('SELECT count(*) FROM calls WHERE job_id=?', (job_id,)).fetchone()[0]
    return dict(job_id=job_id, name=row['name'], state=row['state'], error=row['error'], methods=profile['methods'],
                fake=profile['fake'], model=profile['model']['model'], selection=profile['selection'],
                samples=manifest['sample_count'], actual_calls=using,
                planned_calls=manifest['sample_count'] * sum(a * r for a, r in (METHODS[m] for m in profile['methods'])),
                counts=counts, profile_sha256=profile['profile_sha256'], scoring_version=profile['scoring'],
                manifest_sha256=manifest['manifest_sha256'],
                dataset_revision=manifest['dataset_revision'], quotas=manifest['sampling']['quotas'],
                seed=manifest['sampling']['seed'])


def list_jobs():
    with connection(jobs_root()) as db:
        return [_summary(db, row, json.loads(row['profile']))
                for row in db.execute('SELECT * FROM jobs ORDER BY rowid DESC')]


def view_job(job_id):
    root = jobs_root()
    lock_path = _lock_path(root, job_id)
    with connection(root) as db:
        row, profile = _job(db, job_id)
        if row['state'] in ('running', 'stopping'):
            try:
                with _lock(lock_path):
                    db.execute('BEGIN IMMEDIATE')
                    changed = db.execute("""UPDATE jobs SET state='interrupted',error=?
                                            WHERE id=? AND state IN ('running','stopping')""",
                                         ('后台执行已退出，已保存的请求不会自动重发。', job_id))
                    if changed.rowcount:
                        db.execute("UPDATE tasks SET state='interrupted' WHERE job_id=? AND state='running'", (job_id,))
                        db.execute("UPDATE calls SET state='unknown' WHERE job_id=? AND state='started'", (job_id,))
                    db.commit()
            except EvaluationFailure:
                pass
            row, profile = _job(db, job_id)
        db.execute('BEGIN')
        row, profile = _job(db, job_id)
        summary = _summary(db, row, profile)
        records = {record['question_id']: record for record in profile['manifest']['records']}
        results = []
        for task in db.execute("SELECT * FROM tasks WHERE job_id=? AND state!='pending' ORDER BY rowid", (job_id,)):
            record = records[task['qid']]
            result = json.loads(task['result']) if task['result'] else {}
            rounds = result.get('scores', [])
            results.append(dict(qid=str(task['qid']), repeat=0, method=task['method'], status=task['state'],
                                category=record['category'], question=question_text(record), gold=record['answer'],
                                outputs=[], round_scores=rounds, duration_ms=result.get('duration_ms'),
                                scores={'fixed': rounds[-1] if rounds else {'correct': 0}}))
        usage_rows = {usage['method']: usage for usage in db.execute("""
            SELECT method, count(*) AS calls, sum(state='unknown') AS unknown,
                sum(json_extract(response, '$.usage.prompt_tokens')) AS prompt_tokens,
                sum(json_extract(response, '$.usage.completion_tokens')) AS completion_tokens
            FROM calls WHERE job_id=? GROUP BY method
            """, (job_id,))}
        method_metrics = {}
        for method in summary['methods']:
            usage = dict(usage_rows[method]) if method in usage_rows else {}
            method_metrics[method] = dict(calls=usage.get('calls', 0),
                                          usage={key: usage.get(key) for key in ('prompt_tokens', 'completion_tokens')})
        total_usage = {}
        for key in ('prompt_tokens', 'completion_tokens'):
            known = [usage[key] for usage in usage_rows.values() if usage[key] is not None]
            total_usage[key] = sum(known) if known else None
        return dict(summary, results=results, method_metrics=method_metrics,
                    report=dict(usage=total_usage, unknown_requests=sum(row['unknown'] for row in usage_rows.values())))


def trace(job_id, qid, method):
    root = jobs_root()
    _lock_path(root, job_id)
    with connection(root) as db:
        _job(db, job_id)
        rows = db.execute("""SELECT * FROM calls WHERE job_id=? AND qid=? AND method=?
                             ORDER BY round,agent""", (job_id, qid, method))
        return [dict(task_key=f'{method}:0:{qid}', agent=row['agent'], round=row['round'], attempt=1,
                     status=row['state'], duration_ms=row['duration_ms'], request=json.loads(row['request']),
                     response=json.loads(row['response']) if row['response'] else None, error=row['error'])
                for row in rows]
