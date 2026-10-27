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
from src.evaluation.experiment_config import ROOT, ExperimentConfig, ModelConfig, digest
from src.evaluation.experiment_methods import method_definition
from src.evaluation.experiment_store import utcnow
from src.knowledge.hotpotqa_context import runtime_input
from src.runtime.system_store import SystemStore, fingerprint


class EvaluationFailure(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


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
    targets: list[Target] = Field(min_length=1, max_length=6)
    context_mode: Literal['question_context', 'system_sources'] = 'question_context'
    sample_size: int | None = Field(default=None, ge=1, le=10000, strict=True)
    seed: int = Field(default=42, strict=True)
    repeats: int = Field(default=1, ge=1, le=3, strict=True)
    model: ModelConfig | None = None
    top_k: int = Field(default=3, ge=1, le=50, strict=True)
    expert_count: int = Field(default=2, ge=2, le=3, strict=True)


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
    for agent_id, source in snapshot.get('sources', {}).items():
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
            ''')

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

    def import_bank(self, name, description, value, bank_id=None):
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
                            records=records, sha256=digest(records))
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
                            contextual=sum('context' in r for r in records), preview=records[:3])
        return document

    def banks(self):
        with self.connection() as db:
            keys = db.execute('SELECT bank_id,version FROM bank_versions ORDER BY created_at DESC,version DESC').fetchall()
        return [self.bank(r['bank_id'], r['version'], public=True) for r in keys]

    def create_job(self, fields: JobRequest, systems: SystemStore):
        if not fields.name.strip():
            raise EvaluationFailure('任务名称不能为空。')
        bank = self.bank(fields.bank_id, fields.bank_version)
        records = bank['records']
        if fields.sample_size and fields.sample_size > len(records):
            raise EvaluationFailure('抽样数量超过问题库题数。')
        records = sorted(records, key=lambda r: digest([fields.seed, r['_id']]))[:fields.sample_size or len(records)]
        if fields.context_mode == 'question_context' and any('context' not in r for r in records):
            raise EvaluationFailure('逐题材料模式要求每道题都提供 context。')
        config = ExperimentConfig(dataset='unused', split_directory='unused', output='unused',
                                  model=fields.model or ModelConfig(), top_k=fields.top_k, expert_count=fields.expert_count)
        job_id = 'eval_' + uuid4().hex
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
                snapshot = systems.run_snapshot(target.system_id, target.version)
                if snapshot.get('schema_version') != '3' or snapshot['protocol_document']['spec'].get('language_version') != '2':
                    raise EvaluationFailure('首版批测支持已发布的 schema-3 / V2 用户系统。')
                original = snapshot['config_sha256']
                if fields.context_mode == 'system_sources':
                    freeze_materials(snapshot, self.root / 'jobs' / job_id / 'materials')
                if fields.model:
                    for instance in snapshot['instances']:
                        if instance.get('model_config'):
                            instance['model_config'].update(model_name=fields.model.name, temperature=fields.model.temperature,
                                                            max_tokens=fields.model.max_tokens)
                            snapshot['agents'][instance['agent_id']]['model_config'].update(instance['model_config'])
                    for agent in snapshot['agents'].values():
                        if agent.get('model_config'):
                            agent['failure_handling'] = {'max_retries': fields.model.output_retries}
                snapshot['config_sha256'] = fingerprint(snapshot)
                method_id = 'system_' + key[:16]
                definition = dict(method_id=method_id, label=f"{snapshot['system_ref']['name']} · {target.version}",
                                  snapshot=snapshot, source=dict(kind='published_system', system_id=target.system_id,
                                  version=target.version, original_sha256=original, model_override=fields.model is not None))
                definition['sha256'] = digest(definition)
            definitions[definition['method_id']] = definition
        frozen = dict(schema_version=1, request=fields.model_dump(), bank={k: v for k, v in bank.items() if k != 'records'},
                      records=records, definitions=definitions, identity=identity(), repeats=fields.repeats,
                      planned_tasks=len(records) * len(definitions) * fields.repeats)
        if frozen['planned_tasks'] > 30000:
            raise EvaluationFailure('首版每个任务最多 30,000 次问答，请减少样本或重复数。')
        frozen['sha256'] = digest(frozen)
        with self.connection() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (job_id, fields.name.strip(), 'draft', json.dumps(frozen, ensure_ascii=False), utcnow(), utcnow(), None))
        return self.job(job_id)

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
        return dict(job_id=job_id, name=row['name'], status=row['status'], created_at=row['created_at'],
                    updated_at=row['updated_at'], error=row['error'], bank=frozen['bank'],
                    targets=[dict(method_id=k, label=v['label'], source=v['source']) for k, v in frozen['definitions'].items()],
                    context_mode=frozen['request']['context_mode'], model_override=frozen['request']['model'],
                    repeats=frozen['repeats'], sample_count=len(frozen['records']), progress=progress,
                    fingerprint=frozen['sha256'], report=report)

    def jobs(self):
        with self.connection() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM jobs ORDER BY created_at DESC')]
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
            current = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
            if current not in ('draft', 'paused', 'interrupted', 'error', 'completed'):
                raise EvaluationFailure('该任务已在排队或运行。', 409)
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','stopping')").fetchone():
                raise EvaluationFailure('首版同一时间执行一个评测任务，请先等待或停止当前任务。', 409)
            db.execute("UPDATE jobs SET status='queued',error=NULL,updated_at=? WHERE id=?", (utcnow(), job_id))
