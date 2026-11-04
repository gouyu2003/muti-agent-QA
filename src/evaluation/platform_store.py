"""Versioned question banks and frozen platform jobs, isolated from CLI experiments."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.evaluation.experiment import identity
from src.evaluation.experiment_config import ROOT, ExperimentConfig, ExperimentLimits, ModelConfig, digest
from src.evaluation.experiment_methods import method_definition
from src.evaluation.experiment_store import utcnow
from src.knowledge.hotpotqa_context import runtime_input
from src.runtime.system_store import SystemStore, fingerprint


RETRYABLE_STATUSES = ('failed', 'interrupted', 'cancelled')


class EvaluationFailure(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def published_snapshot(systems, system_id, version):
    """Use the same validation as interactive runs, without migrating saved releases."""
    try:
        snapshot = systems.run_snapshot(system_id, version)
    except (ValueError, OSError) as exc:
        raise EvaluationFailure(f'该系统版本暂不可评测：{exc}') from exc
    except (KeyError, TypeError) as exc:
        raise EvaluationFailure('该系统版本暂不可评测：保存的配置不完整。') from exc
    if snapshot.get('schema_version') not in ('1', '2', '3'):
        raise EvaluationFailure('该系统版本暂不可评测：当前运行服务不支持此配置格式。')
    return snapshot


class Target(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['builtin', 'system']
    method: Literal['single_rag', 'pipeline_fixed', 'pipeline_adaptive', 'multi_expert', 'lead', 'debate'] | None = None
    system_id: str | None = None
    version: str | None = Field(default=None, pattern=r'^v[1-9][0-9]*$')

    @model_validator(mode='after')
    def selection(self):
        if self.kind == 'builtin':
            if not self.method or self.system_id or self.version:
                raise ValueError('标准基线需要 method，不接受系统版本。')
        elif not self.system_id or not self.version or self.method:
            raise ValueError('用户系统需要 system_id 和固定发布版本。')
        return self


class JobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    bank_id: str
    bank_version: int = Field(ge=1, strict=True)
    targets: list[Target] = Field(min_length=1, max_length=12)
    purpose: Literal['smoke', 'development', 'evaluation'] = 'development'
    manifest_id: str | None = None
    preflight_token: str | None = None
    saved_plan_id: str | None = None
    copied_from_job_id: str | None = None
    context_mode: Literal['question_context', 'system_sources'] = 'question_context'
    sample_size: int | None = Field(default=None, ge=1, le=10000, strict=True)
    seed: int = Field(default=42, strict=True)
    repeats: int = Field(default=1, ge=1, le=100, strict=True)
    model: ModelConfig | None = None
    top_k: int = Field(default=3, ge=1, le=50, strict=True)
    expert_count: int = Field(default=2, ge=2, le=3, strict=True)
    execution_mode: Literal['sequential', 'parallel'] = 'sequential'
    retrieval_retries: int = Field(default=3, ge=0, le=3, strict=True)
    plan_retries: int = Field(default=2, ge=0, le=2, strict=True)
    lead_rounds: int = Field(default=2, ge=1, le=4, strict=True)
    debate_rounds: int = Field(default=2, ge=1, le=5, strict=True)
    limits: ExperimentLimits = Field(default_factory=ExperimentLimits)
    primary_metric: Literal['em', 'f1', 'sp_em', 'sp_f1', 'joint_em', 'joint_f1'] = 'f1'
    bootstrap_samples: int = Field(default=2000, ge=100, le=10000, strict=True)
    reference_index: int | None = Field(default=None, ge=0, strict=True)


def validate_questions(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 10000:
        raise EvaluationFailure('问题库应为含 1～10,000 条记录的 JSON 数组。')
    records, ids = [], set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise EvaluationFailure(f'第 {index + 1} 条不是对象。')
        sample_id = item.get('_id', item.get('id', f'q{index + 1}'))
        question, answer = item.get('question'), item.get('answer')
        if (not isinstance(sample_id, str) or not sample_id.strip() or len(sample_id) > 200
                or sample_id in ids or not isinstance(question, str) or not question.strip() or len(question) > 2000):
            raise EvaluationFailure(f'第 {index + 1} 条的问题或 ID 无效，ID 不得重复。')
        if answer is not None and (not isinstance(answer, str) or not answer.strip()):
            raise EvaluationFailure(f'第 {index + 1} 条 answer 必须为非空字符串，或省略。')
        record = dict(_id=sample_id, question=question.strip(), answer=answer, type=item.get('type', 'custom'))
        if not isinstance(record['type'], str) or len(record['type']) > 100:
            raise EvaluationFailure('题型必须为短字符串。')
        context = item.get('context')
        facts = item.get('supporting_facts')
        if context is not None:
            try:
                record['context'] = runtime_input(dict(_id=sample_id, question=question, context=context))['context']
            except (ValueError, TypeError, KeyError) as exc:
                raise EvaluationFailure(f'第 {index + 1} 条 context 格式无效。') from exc
        if facts is not None:
            paragraphs = dict(record.get('context', []))
            if not isinstance(facts, list) or not facts or any(
                not isinstance(f, list) or len(f) != 2 or not isinstance(f[0], str)
                or type(f[1]) is not int or f[0] not in paragraphs or not 0 <= f[1] < len(paragraphs[f[0]])
                for f in facts
            ):
                raise EvaluationFailure(f'第 {index + 1} 条支撑句坐标无效；请先核查数据并记录处理原因。')
            record['supporting_facts'] = facts
        records.append(record)
        ids.add(sample_id)
    return records


def copy_checked(source, destination, expected):
    raw = Path(source).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise EvaluationFailure('固定知识源在准备任务时发生变化，请重新核查。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
            raise EvaluationFailure('任务材料副本校验失败。')
        return
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='copy_', delete=False) as handle:
            temp = Path(handle.name)
            handle.write(raw)
        temp.replace(destination)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def freeze_materials(snapshot, directory):
    """Pin corpus/index bytes locally so later deletion of a system cannot break a job."""
    directory.mkdir(parents=True, exist_ok=True)
    pinned = {}
    sources = snapshot.get('sources') or {
        key: snapshot['source'] for key, agent in snapshot['agents'].items()
        if snapshot.get('source') and agent.get('role') in ('retriever', 'expert')
    }
    for agent_id, source in sources.items():
        local = deepcopy(source)
        for path_key, hash_key in [('resolved_path', 'sha256'), ('index_path', 'index_sha256')]:
            if path_key not in source:
                continue
            path = directory / f'{source[hash_key]}.json'
            copy_checked(source[path_key], path, source[hash_key])
            local[path_key] = str(path.resolve())
        for document in source.get('files', []):
            name = document['original_name']
            if Path(name).name != name or '/' in name or '\\' in name:
                raise EvaluationFailure('知识源原件名称无效。')
            copy_checked(Path(source['resolved_path']).parent / 'originals' / name,
                         directory / 'originals' / name, document['sha256'])
        pinned[agent_id] = local
    snapshot['sources'] = pinned
    if snapshot.get('source') and pinned:
        matching = next((s for s in pinned.values() if s['source_id'] == snapshot['source']['source_id']), None)
        if matching:
            snapshot['source'] = deepcopy(matching)


class EvaluationStore:
    def __init__(self, root=None):
        self.root = Path(root or os.getenv('MAQA_EVALUATIONS_DIR') or ROOT / 'data/evaluations').resolve()
        for name in ('src', 'backend', 'frontend', '.git', 'data/runtime', 'data/systems', 'data/knowledge', 'data/experiments'):
            protected = ROOT / name
            if self.root == protected or protected in self.root.parents or self.root in protected.parents:
                raise EvaluationFailure('评测存储不能与源码、日常数据或 CLI 实验目录重叠。')
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS banks (id TEXT PRIMARY KEY, name TEXT, description TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS bank_versions (bank_id TEXT, version INTEGER, document TEXT, created_at TEXT,
                    PRIMARY KEY(bank_id, version));
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, name TEXT, status TEXT, document TEXT,
                    created_at TEXT, updated_at TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS manifests (id TEXT PRIMARY KEY, document TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS exposures (bank_id TEXT, sample_id TEXT, reason TEXT, created_at TEXT,
                    PRIMARY KEY(bank_id,sample_id));
                CREATE TABLE IF NOT EXISTS platform_meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS saved_plans (id TEXT PRIMARY KEY,name TEXT,document TEXT,sha256 TEXT,source_job_id TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS job_catalog (job_id TEXT PRIMARY KEY,archived INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS cli_archives (id TEXT PRIMARY KEY,document TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS cli_results (archive_id TEXT,position INTEGER,document TEXT,
                    PRIMARY KEY(archive_id,position));
                CREATE INDEX IF NOT EXISTS cli_results_task ON cli_results(archive_id,
                    json_extract(document,'$.sample_id'),json_extract(document,'$.method_id'),
                    json_extract(document,'$.repeat'),json_extract(document,'$.attempt'));
            ''')
            # Seed known historical platform trials once. Existing frozen jobs remain byte-for-byte unchanged.
            if not db.execute("SELECT 1 FROM platform_meta WHERE key='exposures-v1'").fetchone():
                db.execute('''INSERT OR IGNORE INTO exposures
                    SELECT json_extract(j.document, '$.bank.bank_id'), json_extract(r.value, '$._id'),
                           '既有平台开发或未标用途的试跑任务', j.created_at
                    FROM jobs j, json_each(j.document, '$.records') r
                    WHERE COALESCE(json_extract(j.document, '$.request.purpose'), 'development') != 'evaluation' ''')
                db.execute("INSERT OR IGNORE INTO platform_meta VALUES ('exposures-v1','done')")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.root / 'platform.sqlite3', timeout=20)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def import_bank(self, name, description, value, bank_id=None, *, raw_sha256=None):
        name = name.strip()
        if not 1 <= len(name) <= 100 or len(description) > 2000:
            raise EvaluationFailure('问题库名称需为 1～100 个字符，说明不超过 2,000 字符。')
        records = validate_questions(value)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if bank_id:
                if not db.execute('SELECT 1 FROM banks WHERE id=?', (bank_id,)).fetchone():
                    raise EvaluationFailure('问题库不存在。', 404)
            else:
                bank_id = 'bank_' + uuid4().hex
                db.execute('INSERT INTO banks VALUES (?,?,?,?)', (bank_id, name, description, utcnow()))
            version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM bank_versions WHERE bank_id=?', (bank_id,)).fetchone()[0]
            document = dict(bank_id=bank_id, version=version, name=name, description=description,
                            records=records, sha256=digest(records), raw_sha256=raw_sha256)
            db.execute('INSERT INTO bank_versions VALUES (?,?,?,?)', (bank_id, version, json.dumps(document, ensure_ascii=False), utcnow()))
        return self.bank(bank_id, version, public=True)

    def bank(self, bank_id, version, public=False):
        with self.connection() as db:
            row = db.execute('SELECT document FROM bank_versions WHERE bank_id=? AND version=?', (bank_id, version)).fetchone()
        if not row:
            raise EvaluationFailure('问题库版本不存在。', 404)
        document = json.loads(row[0])
        if digest(document['records']) != document['sha256']:
            raise EvaluationFailure('问题库版本校验失败。')
        if public:
            records = document.pop('records')
            document.update(count=len(records), labelled=sum(r['answer'] is not None for r in records),
                            contextual=sum('context' in r for r in records), preview=[])
        return document

    def banks(self):
        with self.connection() as db:
            keys = db.execute('SELECT bank_id,version FROM bank_versions ORDER BY created_at DESC,version DESC').fetchall()
        return [self.bank(r['bank_id'], r['version'], public=True) for r in keys]

    def prepare_job(self, fields: JobRequest, systems: SystemStore):
        if not fields.name.strip():
            raise EvaluationFailure('任务名称不能为空。')
        if fields.saved_plan_id:
            from src.evaluation.platform_workspace import get_plan
            get_plan(self, fields.saved_plan_id)
        bank = self.bank(fields.bank_id, fields.bank_version)
        from src.evaluation.platform_datasets import job_records
        records, manifest = job_records(self, fields, bank)
        if fields.context_mode == 'question_context' and any('context' not in r for r in records):
            raise EvaluationFailure('逐题材料模式要求每道题都提供 context。')
        config = ExperimentConfig(dataset='unused', split_directory='unused', output='unused',
                                  model=fields.model or ModelConfig(), top_k=fields.top_k, expert_count=fields.expert_count,
                                  execution_mode=fields.execution_mode, retrieval_retries=fields.retrieval_retries,
                                  plan_retries=fields.plan_retries, lead_rounds=fields.lead_rounds,
                                  debate_rounds=fields.debate_rounds, limits=fields.limits)
        if fields.primary_metric.startswith(('sp_', 'joint_')) and (fields.context_mode != 'question_context' or any(not r.get('supporting_facts') for r in records)):
            raise EvaluationFailure('支撑事实或联合指标作为主指标时，需使用逐题 context 且每题提供 supporting_facts。')
        definitions = {}
        selections = set()
        for target in fields.targets:
            key = digest(target.model_dump())
            if key in selections:
                raise EvaluationFailure('同一基线或系统版本不能重复选择。')
            selections.add(key)
            if target.kind == 'builtin':
                if fields.context_mode != 'question_context':
                    raise EvaluationFailure('标准基线需要使用问题自带的逐题材料。')
                definition = method_definition(config, target.method)
                definition['label'] = target.method
            else:
                snapshot = published_snapshot(systems, target.system_id, target.version)
                original = snapshot['config_sha256']
                if fields.model:
                    for instance in snapshot.get('instances', []):
                        if instance.get('model_config'):
                            instance['model_config'].update(model_name=fields.model.name, temperature=fields.model.temperature,
                                                            max_tokens=fields.model.max_tokens)
                            snapshot['agents'][instance['agent_id']]['model_config'].update(instance['model_config'])
                    for agent in snapshot['agents'].values():
                        if agent.get('model_config'):
                            agent['model_config'].update(model_name=fields.model.name, temperature=fields.model.temperature,
                                                         max_tokens=fields.model.max_tokens)
                            agent['failure_handling'] = {'max_retries': fields.model.output_retries}
                snapshot['config_sha256'] = fingerprint(snapshot)
                method_id = 'system_' + key[:16]
                definition = dict(method_id=method_id, label=f"{snapshot['system_ref']['name']} · {target.version}",
                                  snapshot=snapshot, source=dict(kind='published_system', system_id=target.system_id,
                                  version=target.version, original_sha256=original, model_override=fields.model is not None))
                definition['sha256'] = digest(definition)
            definitions[definition['method_id']] = definition
        if fields.reference_index is not None and fields.reference_index >= len(definitions):
            raise EvaluationFailure('比较基准不在所选对象中。')
        reference = list(definitions)[fields.reference_index] if fields.reference_index is not None else (
            'single_rag' if 'single_rag' in definitions else next(iter(definitions)))
        frozen = dict(schema_version=2, request=fields.model_dump(exclude={'preflight_token'}), manifest=manifest,
                      reference_method=reference, bank={k: v for k, v in bank.items() if k != 'records'},
                      records=records, definitions=definitions, identity=identity(), repeats=fields.repeats,
                      planned_tasks=len(records) * len(definitions) * fields.repeats)
        if frozen['planned_tasks'] > 30000:
            raise EvaluationFailure('首版每个任务最多 30,000 次问答，请减少样本或重复数。')
        return frozen

    def preflight(self, fields, systems):
        frozen = self.prepare_job(fields, systems)
        return dict(token=digest(frozen), planned_tasks=frozen['planned_tasks'], sample_count=len(frozen['records']),
                    data_sha256=digest(frozen['records']), purpose=fields.purpose,
                    manifest=frozen['manifest'], request=frozen['request'], reference_method=frozen['reference_method'],
                    targets=[dict(method_id=k, label=d['label'], source=d['source'],
                        models=[dict(agent_id=key, model=a.get('model_config'),
                                     retrieval=a.get('retrieval_config')) for key, a in d['snapshot']['agents'].items()],
                        workflow=d['snapshot'].get('workflow_spec'),
                        limits=d['snapshot'].get('protocol_document', {}).get('spec', {}).get('limits'),
                        sha256=d['sha256']) for k, d in frozen['definitions'].items()], model_calls=0)

    def create_job(self, fields: JobRequest, systems: SystemStore):
        frozen = self.prepare_job(fields, systems)
        if (fields.purpose == 'evaluation' and not fields.preflight_token) or (
                fields.preflight_token and fields.preflight_token != digest(frozen)):
            raise EvaluationFailure('请重新执行启动前校验并确认最新方案；数据、配置或环境发生变化。', 409)
        job_id = 'eval_' + uuid4().hex
        if fields.context_mode == 'system_sources':
            for definition in frozen['definitions'].values():
                freeze_materials(definition['snapshot'], self.root / 'jobs' / job_id / 'materials')
                definition['snapshot']['config_sha256'] = fingerprint(definition['snapshot'])
                definition['sha256'] = digest({k: v for k, v in definition.items() if k != 'sha256'})
        frozen['sha256'] = digest(frozen)
        with self.connection() as db:
            from src.evaluation.platform_datasets import check_overlap, register
            db.execute('BEGIN IMMEDIATE')
            if fields.purpose == 'evaluation':
                check_overlap(db, fields.bank_id, [r['_id'] for r in frozen['records']])
            else:
                register(db, fields.bank_id, [r['_id'] for r in frozen['records']], f'冻结{fields.purpose}任务：{fields.name}')
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (job_id, fields.name.strip(), 'draft', json.dumps(frozen, ensure_ascii=False), utcnow(), utcnow(), None))
        return self.job(job_id)

    def check_job_data(self, job_id):
        _, frozen = self.frozen(job_id)
        if frozen['request'].get('purpose') == 'evaluation':
            from src.evaluation.platform_datasets import check_overlap
            with self.connection() as db:
                check_overlap(db, frozen['bank']['bank_id'], [r['_id'] for r in frozen['records']])

    def frozen(self, job_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise EvaluationFailure('评测任务不存在。', 404)
        frozen = json.loads(row['document'])
        if digest({k: v for k, v in frozen.items() if k != 'sha256'}) != frozen['sha256']:
            raise EvaluationFailure('冻结任务校验失败，不能执行。')
        return dict(row), frozen

    def job_directory(self, job_id):
        self.frozen(job_id)  # Never compose a path from an unvalidated caller ID.
        return self.root / 'jobs' / job_id

    def retryable_count(self, job_id):
        row, _ = self.frozen(job_id)
        directory = self.root / 'jobs' / job_id
        ledger = directory / 'tasks.sqlite3'
        if not ledger.is_file():
            return 0
        with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True) as db:
            try:
                latest = db.execute('''SELECT a.run_id, a.status FROM attempts a
                    WHERE a.attempt = (SELECT MAX(b.attempt) FROM attempts b WHERE b.task_id = a.task_id)''').fetchall()
            except sqlite3.OperationalError:
                return 0  # Worker is still creating its tables.
        count = sum(status in RETRYABLE_STATUSES for _, status in latest)
        if row['status'] in ('queued', 'running', 'stopping'):
            return count
        # A dead worker can leave the ledger running after Runtime persisted a terminal result.
        # Preview recovery without changing the ledger or replaying a completed request.
        unfinished = [run_id for run_id, status in latest if status == 'running']
        runtime = directory / 'runs.sqlite3'
        if not unfinished or not runtime.is_file():
            return count + len(unfinished)
        with sqlite3.connect(runtime.as_uri() + '?mode=ro', uri=True) as db:
            for run_id in unfinished:
                saved = db.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone()
                if not saved or saved[0] != 'completed':
                    count += 1
        return count

    def job(self, job_id):
        row, frozen = self.frozen(job_id)
        directory = self.root / 'jobs' / job_id
        report_path = directory / 'summary.json'
        report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else None
        progress = dict(attempted=0, planned=frozen['planned_tasks'], completed=0, failed=0, interrupted=0)
        ledger = directory / 'tasks.sqlite3'
        if ledger.exists():
            with sqlite3.connect(ledger) as db:
                try:
                    counts = db.execute('SELECT status,COUNT(*) FROM attempts WHERE attempt=1 GROUP BY status').fetchall()
                except sqlite3.OperationalError:
                    counts = []  # Worker is still creating its tables.
            progress.update({status: count for status, count in counts})
            progress['attempted'] = sum(count for _, count in counts)
        with self.connection() as db:
            archived = db.execute('SELECT archived FROM job_catalog WHERE job_id=?', (job_id,)).fetchone()
        return dict(job_id=job_id, name=row['name'], status=row['status'], created_at=row['created_at'], archived=bool(archived and archived[0]),
                    updated_at=row['updated_at'], error=row['error'], bank=frozen['bank'],
                    targets=[dict(method_id=k, label=v['label'], source=v['source']) for k, v in frozen['definitions'].items()],
                    context_mode=frozen['request']['context_mode'], model_override=frozen['request']['model'],
                    repeats=frozen['repeats'], sample_count=len(frozen['records']), progress=progress,
                    fingerprint=frozen['sha256'], report=report, retryable_count=self.retryable_count(job_id),
                    plan=dict(request=frozen['request'], manifest=frozen.get('manifest'),
                              reference_method=frozen.get('reference_method')))

    def jobs(self, include_archived=False):
        with self.connection() as db:
            ids = [r[0] for r in db.execute('SELECT j.id FROM jobs j LEFT JOIN job_catalog c ON c.job_id=j.id' +
                ('' if include_archived else ' WHERE COALESCE(c.archived,0)=0') + ' ORDER BY j.created_at DESC')]
        return [self.job(i) for i in ids]

    def state(self, job_id, status, error=None):
        self.frozen(job_id)
        with self.connection() as db:
            db.execute('UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?', (status, error, utcnow(), job_id))

    def transition(self, job_id, status, allowed):
        self.frozen(job_id)
        with self.connection() as db:
            placeholders = ','.join('?' for _ in allowed)
            return db.execute(f'UPDATE jobs SET status=?,updated_at=? WHERE id=? AND status IN ({placeholders})',
                              (status, utcnow(), job_id, *allowed)).rowcount == 1

    def queue(self, job_id):
        row, frozen = self.frozen(job_id)
        if identity() != frozen['identity']:
            raise EvaluationFailure('源码、提示词或依赖已变化，请新建评测任务。', 409)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM job_catalog WHERE job_id=? AND archived=1', (job_id,)).fetchone():
                raise EvaluationFailure('任务已归档，请先取消归档再启动。', 409)
            current = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
            if current not in ('draft', 'paused', 'interrupted', 'error', 'completed'):
                raise EvaluationFailure('该任务已在排队或运行。', 409)
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','stopping')").fetchone():
                raise EvaluationFailure('首版同一时间执行一个评测任务，请先等待或停止当前任务。', 409)
            db.execute("UPDATE jobs SET status='queued',error=NULL,updated_at=? WHERE id=?", (utcnow(), job_id))
