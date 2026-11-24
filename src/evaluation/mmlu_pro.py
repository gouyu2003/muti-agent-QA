"""Fixed-choice MMLU-Pro jobs with a durable, per-generation trace."""
from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Thread
from time import perf_counter
from uuid import uuid4

from src.evaluation.experiment_config import ROOT, digest
from src.evaluation.experiment_store import utcnow
from src.evaluation.gsm8k_platform import _lock
from src.evaluation.platform_store import EvaluationFailure, EvaluationStore
from src.llm import DEFAULT_GENERATION_MODEL

METHODS = {'single': (1, 1), 'reflection': (1, 2), 'majority': (6, 1), 'debate': (3, 2)}
MANIFESTS = {
    'smoke': ('smoke-5', 'validation', 'cdf15f53eb0e269114c02689d801e0a8777ed1419d72446f127988cbd5da6316'),
    'development': ('development-100', 'test', '8a4f1bdcfcc16d997c9d8b9338d7697872e89530d00e25abbac0918b2406f359'),
    'calibration-60': ('calibration-60', 'test', 'ad7aabe16925fd04c7e983562ac62639027249e38cb529bc4f478be957fce473'),
    'comparison-100': ('comparison-100', 'test', 'dc980e47c6578c9651e9158df3514b3d3dce9180d47c2fce433af277cf4d4c6d'),
}
LETTERS = 'ABCDEFGHIJ'
SCORING_VERSION = 'mmlu-pro-final-choice-v2'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
# The previous runner may be resumed only while its request and scoring code matches.
PRE_RECOVERY_CODE_SHA256 = '82760760e927458cf0c2adb2561d00b5ec21c5b794f2596f0df2deae6d8da961'
PRE_RECOVERY_EXECUTION_SHA256 = 'e1102859028b592347a6f9ee9fe91824cee1ec4e89924343f1a9d764a748b93f'
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
                CREATE TABLE IF NOT EXISTS pending_resumes (
                    job_id TEXT NOT NULL, admitted_at TEXT NOT NULL, previous_error TEXT,
                    profile_sha256 TEXT NOT NULL, execution_code_sha256 TEXT NOT NULL,
                    scheduled_calls INTEGER NOT NULL,
                    PRIMARY KEY (job_id, admitted_at)
                );
                CREATE TABLE IF NOT EXISTS superseded_calls (
                    job_id TEXT NOT NULL, qid INTEGER NOT NULL, method TEXT NOT NULL,
                    round INTEGER NOT NULL, agent INTEGER NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL,
                    response TEXT, error TEXT, duration_ms INTEGER,
                    superseded_at TEXT NOT NULL, execution_code_sha256 TEXT NOT NULL,
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
    if selection == 'comparison-100' and set(methods) != METHODS.keys():
        raise EvaluationFailure('100 题四组对照须同时选择四种方法。')
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


def pauses_after_single(profile):
    methods = profile['methods']
    return profile['selection'] in ('development', 'comparison-100') and len(methods) > 1 and methods[0] == 'single'


def _execution_signature():
    functions = (parse_choice, score_round, question_text, model_client, generate, run_task, pauses_after_single)
    return digest(dict(methods=METHODS, model=MODEL, scoring=SCORING_VERSION, letters=LETTERS, final=FINAL,
                       functions=[inspect.getsource(function) for function in functions]))


def _worker(root, job_id, profile, client, guard):
    records = {record['question_id']: record for record in profile['manifest']['records']}
    try:
        with connection(root) as db:
            tasks = db.execute("SELECT qid,method FROM tasks WHERE job_id=? AND state='pending' ORDER BY rowid",
                               (job_id,)).fetchall()
            checkpoint = pauses_after_single(profile) and tasks and tasks[0]['method'] == 'single'
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
            unresolved = db.execute("""SELECT count(*) FROM tasks
                WHERE job_id=? AND state IN ('interrupted','failed')""", (job_id,)).fetchone()[0]
            state = 'interrupted' if unresolved else 'completed'
            note = f'待执行题目已跑完，仍有 {unresolved} 个中断或失败任务待核对。' if unresolved else None
            db.execute('UPDATE jobs SET state=?,error=? WHERE id=?', (state, note, job_id))
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


def start_job(job_id, allow_model_calls, *, resume_pending=False):
    root = jobs_root()
    guard = _lock(_lock_path(root, job_id))
    guard.__enter__()
    client = None
    try:
        with connection(root) as db:
            row, profile = _job(db, job_id)
            summary = _summary(db, row, profile)
            if resume_pending:
                if not summary['can_resume_pending']:
                    raise EvaluationFailure('当前实验没有可以续跑的待执行题目。', 409)
                existing_call = db.execute("""
                    SELECT 1 FROM tasks AS t JOIN calls AS c USING (job_id,qid,method)
                    WHERE t.job_id=? AND t.state='pending' LIMIT 1
                    """, (job_id,)).fetchone()
                if existing_call:
                    raise EvaluationFailure('待执行任务已有调用记录，请先核对账本。', 409)
            elif row['state'] not in ('draft', 'paused') or summary['can_resume_pending']:
                raise EvaluationFailure('只能启动草稿或继续已暂停的实验。', 409)
            if profile['code_sha256'] != CODE_SHA256:
                compatible = (resume_pending and profile['code_sha256'] == PRE_RECOVERY_CODE_SHA256 and
                              _execution_signature() == PRE_RECOVERY_EXECUTION_SHA256)
                if not compatible:
                    raise EvaluationFailure('执行代码与冻结实验不同，请核对执行版本。', 409)
            if resume_pending and digest({key: value for key, value in profile.items() if key != 'profile_sha256'}) != profile['profile_sha256']:
                raise EvaluationFailure('冻结实验指纹异常，请核对实验记录。', 409)
            if not profile['fake'] and not allow_model_calls:
                raise EvaluationFailure('请确认真实模型调用。')
            client = None if profile['fake'] else model_client()
            if client is not None:
                endpoint = digest(str(client.base_url).rstrip('/'))
                if profile.get('endpoint_sha256', None if resume_pending else endpoint) != endpoint:
                    raise EvaluationFailure('模型端点与首次执行时不同，请检查配置。', 409)
                if 'endpoint_sha256' not in profile:
                    profile['endpoint_sha256'] = endpoint
                    profile['profile_sha256'] = digest({key: value for key, value in profile.items()
                                                        if key != 'profile_sha256'})
                    db.execute('UPDATE jobs SET profile=? WHERE id=?', (json.dumps(profile), job_id))
            if resume_pending:
                db.execute('INSERT INTO pending_resumes VALUES (?,?,?,?,?,?)',
                           (job_id, utcnow(), row['error'], profile['profile_sha256'], CODE_SHA256,
                            summary['next_stage_calls']))
            db.execute("UPDATE jobs SET state='running',error=NULL WHERE id=?", (job_id,))
        thread = Thread(target=_worker, args=(root, job_id, profile, client, guard), daemon=True)
        try:
            thread.start()
        except RuntimeError:
            with connection(root) as db:
                db.execute("UPDATE jobs SET state=?,error=? WHERE id=?",
                           (row['state'], '后台线程启动失败。', job_id))
            raise
    except BaseException:
        try:
            if client is not None:
                client.close()
        finally:
            guard.__exit__(None, None, None)
        raise
    return view_job(job_id)


def supplement_reflection_task(job_id, qid, *, allow_model_calls=False):
    """Make one explicit replacement attempt for the only unfinished Reflection task."""
    method = 'reflection'
    root = jobs_root()
    key = (job_id, qid, method)
    with _lock(_lock_path(root, job_id)), connection(root) as db:
        from src.evaluation.platform_job_actions import job_archived

        if job_archived(EvaluationStore(), job_id):
            raise EvaluationFailure('实验已归档，请先取消归档再补跑。', 409)
        row, profile = _job(db, job_id)
        unfinished = db.execute("""SELECT qid,method,state,result FROM tasks
                                   WHERE job_id=? AND state!='completed'""", (job_id,)).fetchall()
        if row['state'] != 'interrupted' or [tuple(task) for task in unfinished] != [(qid, method, 'interrupted', None)]:
            raise EvaluationFailure('补跑要求原实验只有指定的 Reflection 题中断。', 409)
        original = db.execute("""SELECT round,agent,state,response FROM calls
                                  WHERE job_id=? AND qid=? AND method=?""", key).fetchall()
        if [tuple(call) for call in original] != [(0, 0, 'unknown', None)] or db.execute(
                'SELECT 1 FROM superseded_calls WHERE job_id=? AND qid=? AND method=?', key).fetchone():
            raise EvaluationFailure('只支持首次补跑且原任务只有一条状态不明的首轮调用。', 409)
        if (profile['code_sha256'] not in (PRE_RECOVERY_CODE_SHA256, CODE_SHA256)
                or _execution_signature() != PRE_RECOVERY_EXECUTION_SHA256
                or profile['scoring'] != SCORING_VERSION or profile['model'] != MODEL
                or digest({k: v for k, v in profile.items() if k != 'profile_sha256'}) != profile['profile_sha256']):
            raise EvaluationFailure('冻结实验的执行配置与当前版本不同。', 409)
        record = next(record for record in profile['manifest']['records'] if record['question_id'] == qid)
        if not profile['fake'] and not allow_model_calls:
            raise EvaluationFailure('请确认真实模型调用。')
        client = None if profile['fake'] else model_client()
        try:
            if client is not None and digest(str(client.base_url).rstrip('/')) != profile.get('endpoint_sha256'):
                raise EvaluationFailure('模型端点与首次执行时不同，请检查配置。', 409)
            db.execute('BEGIN IMMEDIATE')
            db.execute("""INSERT INTO superseded_calls
                          SELECT calls.*, ?, ? FROM calls WHERE job_id=? AND qid=? AND method=?""",
                       (utcnow(), CODE_SHA256, *key))
            db.execute('DELETE FROM calls WHERE job_id=? AND qid=? AND method=?', key)
            db.execute("UPDATE tasks SET state='running' WHERE job_id=? AND qid=? AND method=?", key)
            db.execute("UPDATE jobs SET state='running',error=NULL WHERE id=?", (job_id,))
            db.commit()
            try:
                started = perf_counter()
                result = run_task(db, job_id, record, method, profile['model'], client, 0)
                result['duration_ms'] = round((perf_counter() - started) * 1000)
                db.execute("UPDATE tasks SET state='completed',result=? WHERE job_id=? AND qid=? AND method=?",
                           (json.dumps(result), *key))
                db.execute("UPDATE jobs SET state='completed',error=NULL WHERE id=?", (job_id,))
                db.commit()
            except BaseException as exc:
                db.rollback()
                db.execute("""UPDATE calls SET state='unknown'
                              WHERE job_id=? AND qid=? AND method=? AND state='started'""", key)
                db.execute("UPDATE tasks SET state='interrupted' WHERE job_id=? AND qid=? AND method=?", key)
                message = str(exc) if isinstance(exc, EvaluationFailure) else f'补跑中断（{type(exc).__name__}）。'
                db.execute("UPDATE jobs SET state='interrupted',error=? WHERE id=?", (message, job_id))
                db.commit()
                raise
        finally:
            if client is not None:
                client.close()
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
    pending = {}
    for method, state, count in db.execute(
            'SELECT method,state,count(*) FROM tasks WHERE job_id=? GROUP BY method,state', (job_id,)):
        counts[state] += count
        if state == 'pending':
            pending[method] = count
    using = db.execute('SELECT count(*) FROM calls WHERE job_id=?', (job_id,)).fetchone()[0]
    superseded = db.execute('SELECT count(*) FROM superseded_calls WHERE job_id=?', (job_id,)).fetchone()[0]
    resumes = db.execute('SELECT count(*) FROM pending_resumes WHERE job_id=?', (job_id,)).fetchone()[0]
    samples = manifest['sample_count']
    planned_calls = samples * sum(agents * rounds for agents, rounds in (METHODS[m] for m in profile['methods']))
    pending_calls = sum(count * METHODS[method][0] * METHODS[method][1] for method, count in pending.items())
    next_stage_calls = pending['single'] if 'single' in pending and pauses_after_single(profile) else pending_calls
    can_resume_pending = bool(pending) and not counts['running'] and (row['state'] == 'interrupted' or (
        row['state'] == 'paused' and bool(resumes or counts['interrupted'] or counts['failed'])))
    return dict(job_id=job_id, name=row['name'], state=row['state'], error=row['error'], methods=profile['methods'],
                fake=profile['fake'], model=profile['model']['model'], selection=profile['selection'],
                samples=samples, actual_calls=using + superseded, superseded_calls=superseded,
                planned_calls=planned_calls, next_stage_calls=next_stage_calls,
                pending_calls=pending_calls, can_resume_pending=can_resume_pending, pending_resume_count=resumes,
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
            FROM (
                SELECT method,state,response FROM calls WHERE job_id=?
                UNION ALL
                SELECT method,state,response FROM superseded_calls WHERE job_id=?
            ) GROUP BY method
            """, (job_id, job_id))}
        method_metrics = {}
        for method in summary['methods']:
            usage = dict(usage_rows[method]) if method in usage_rows else {}
            method_metrics[method] = dict(calls=usage.get('calls', 0),
                                          usage={key: usage.get(key) for key in ('prompt_tokens', 'completion_tokens')})
        total_usage = {}
        for key in ('prompt_tokens', 'completion_tokens'):
            known = [usage[key] for usage in usage_rows.values() if usage[key] is not None]
            total_usage[key] = sum(known) if known else None
        resumes = [dict(event) for event in db.execute("""
            SELECT admitted_at,previous_error,profile_sha256,execution_code_sha256,scheduled_calls
            FROM pending_resumes WHERE job_id=? ORDER BY admitted_at
            """, (job_id,))]
        return dict(summary, results=results, method_metrics=method_metrics, pending_resumes=resumes,
                    report=dict(usage=total_usage, unknown_requests=sum(row['unknown'] for row in usage_rows.values())))


def trace(job_id, qid, method):
    root = jobs_root()
    _lock_path(root, job_id)
    with connection(root) as db:
        _job(db, job_id)
        key = (job_id, qid, method)
        current = db.execute('SELECT * FROM calls WHERE job_id=? AND qid=? AND method=? ORDER BY round,agent', key)
        history = db.execute('SELECT * FROM superseded_calls WHERE job_id=? AND qid=? AND method=? ORDER BY round,agent',
                             key).fetchall()
        attempts = [(1, history), (2 if history else 1, current)]
        return [dict(task_key=f'{method}:0:{qid}', agent=row['agent'], round=row['round'], attempt=attempt,
                     status=row['state'], duration_ms=row['duration_ms'], request=json.loads(row['request']),
                     response=json.loads(row['response']) if row['response'] else None, error=row['error'])
                for attempt, rows in attempts for row in rows]
