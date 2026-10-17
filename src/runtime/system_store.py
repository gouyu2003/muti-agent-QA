"""Workspace systems: editable saved drafts and immutable published snapshots."""
from __future__ import annotations

from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from src.runtime.system import DEFAULT_CATALOG_PATH, resolve_system, retriever_from_snapshot
from src.runtime.specs import WorkflowSpec


class SystemFailure(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def template_snapshot(*args, **kwargs):
    try:
        return resolve_system(*args, **kwargs).snapshot()
    except (ValueError, OSError) as exc:
        raise SystemFailure(f'所选模板或知识源不可用：{exc}') from exc


def fingerprint(snapshot: dict) -> str:
    body = {k: v for k, v in snapshot.items() if k not in {'config_sha256', 'system_ref'}}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class SystemStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv('MAQA_SYSTEMS_DIR') or Path(__file__).resolve().parents[2] / 'data/systems')
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'systems.sqlite3'
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS systems (
                    system_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    draft TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS releases (
                    system_id TEXT NOT NULL REFERENCES systems(system_id),
                    version INTEGER NOT NULL, revision INTEGER NOT NULL,
                    document TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(system_id, version), UNIQUE(system_id, revision));
            ''')

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.database, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                yield db

    @staticmethod
    def _row(db, system_id):
        row = db.execute('SELECT * FROM systems WHERE system_id=?', (system_id,)).fetchone()
        if row is None:
            raise SystemFailure('问答系统不存在。', 404)
        return row

    def list(self):
        with self.connection() as db:
            ids = [row[0] for row in db.execute('SELECT system_id FROM systems ORDER BY updated_at DESC, system_id')]
        return [self.get(key) for key in ids]

    def get(self, system_id):
        with self.connection() as db:
            row = self._row(db, system_id)
            releases = [json.loads(item[0]) for item in db.execute(
                'SELECT document FROM releases WHERE system_id=? ORDER BY version DESC', (system_id,))]
        return {'system_id': system_id, 'draft_revision': row['revision'], 'draft': json.loads(row['draft']),
                'versions': releases, 'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def _draft(self, system_id, fields, previous=None):
        name = fields['name'].strip()
        if not name or len(name) > 100:
            raise SystemFailure('系统名称需为 1 至 100 个字符。')
        workflow_id, source_id, source_version = (fields.get(k) for k in ('workflow_id', 'source_id', 'source_version'))
        if source_version is not None and not source_id:
            raise SystemFailure('指定知识源版本时必须选择知识源。')
        catalog = os.getenv('MAQA_SYSTEM_CONFIG_PATH') or DEFAULT_CATALOG_PATH
        # Reuse unchanged saved components; a template edit must not silently
        # replace a draft's frozen agent/prompt/protocol configuration.
        base = deepcopy(previous['snapshot']) if previous else template_snapshot(catalog_path=catalog)
        if not previous or previous.get('workflow_id') != workflow_id:
            selected = template_snapshot(workflow_id, catalog_path=catalog)
            base['protocol'], base['workflow_spec'] = selected['protocol'], selected['workflow_spec']
        same_source = previous and (previous.get('source_id'), previous.get('source_version')) == (source_id, source_version)
        if source_id and not same_source:
            selected = template_snapshot(workflow_id, source_id, source_version=source_version, catalog_path=catalog)
            base['source'], base['dataset'] = selected['source'], selected['dataset']
        if source_id and base.get('source', {}).get('source_type') == 'document_corpus' and source_version is None:
            base.pop('source', None)
            base.pop('dataset', None)
        if source_id and 'source' in base:
            source = base['source']
            base['agents']['retriever_v0']['knowledge_sources'] = [{
                'source_id': source_id, 'source_type': source['source_type'],
                **({'source_version': source_version} if source_version is not None else {})}]
        else:
            base.pop('source', None)
            base.pop('dataset', None)
        base.pop('system_ref', None)
        base['system_id'] = system_id
        base['system_version'] = 'draft'
        base['config_sha256'] = fingerprint(base)
        return {'name': name, 'description': fields.get('description', '').strip(),
                'workflow_id': workflow_id, 'source_id': source_id, 'source_version': source_version, 'snapshot': base}

    def create(self, fields):
        system_id = 'sys_' + uuid4().hex
        document = self._draft(system_id, fields)
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            db.execute('INSERT INTO systems VALUES (?, 1, ?, ?, ?)',
                       (system_id, json.dumps(document, ensure_ascii=False), now, now))
        return self.get(system_id)

    def save(self, system_id, expected_revision, fields):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, system_id)
            if row['revision'] != expected_revision:
                raise SystemFailure('草稿已发生修改，请重新读取后再保存。', 409)
            document = self._draft(system_id, fields, json.loads(row['draft']))
            db.execute('UPDATE systems SET revision=revision+1, draft=?, updated_at=? WHERE system_id=?',
                       (json.dumps(document, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), system_id))
        return self.get(system_id)

    @staticmethod
    def validate(document):
        if not document.get('workflow_id') or not document.get('source_id'):
            raise SystemFailure('请先选择协作流程和知识源并保存草稿。')
        snapshot = document['snapshot']
        if 'source' not in snapshot:
            raise SystemFailure('请选择明确的知识源版本并保存草稿。')
        if fingerprint(snapshot) != snapshot['config_sha256']:
            raise SystemFailure('保存的配置校验失败。')
        try:
            WorkflowSpec.model_validate(snapshot['workflow_spec'])
            retriever_from_snapshot(snapshot)
        except (ValueError, OSError) as exc:
            raise SystemFailure('配置或知识源材料校验失败，请检查绑定版本，不能自动切换来源。') from exc

    def publish(self, system_id, expected_revision):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, system_id)
            if row['revision'] != expected_revision:
                raise SystemFailure('草稿已发生修改，请重新读取后再发布。', 409)
            old = db.execute('SELECT document FROM releases WHERE system_id=? AND revision=?',
                             (system_id, expected_revision)).fetchone()
            if old:
                return json.loads(old[0])
            document = json.loads(row['draft'])
            self.validate(document)
            number = db.execute('SELECT COALESCE(MAX(version), 0)+1 FROM releases WHERE system_id=?', (system_id,)).fetchone()[0]
            document.update(version=f'v{number}', draft_revision=expected_revision, created_at=datetime.now(timezone.utc).isoformat())
            document['snapshot']['system_version'] = document['version']
            document['snapshot']['config_sha256'] = fingerprint(document['snapshot'])
            db.execute('INSERT INTO releases VALUES (?, ?, ?, ?, ?)',
                       (system_id, number, expected_revision, json.dumps(document, ensure_ascii=False), document['created_at']))
        return document

    def version(self, system_id, version):
        if not isinstance(version, str) or not version.startswith('v') or not version[1:].isdigit() or int(version[1:]) < 1 or version != f'v{int(version[1:])}':
            raise SystemFailure('发布版本格式应为 v1、v2……。')
        with self.connection() as db:
            self._row(db, system_id)
            row = db.execute('SELECT document FROM releases WHERE system_id=? AND version=?', (system_id, int(version[1:]))).fetchone()
        if row is None:
            raise SystemFailure('发布版本不存在。', 404)
        return json.loads(row[0])

    def run_snapshot(self, system_id, version=None, revision=None):
        if (version is None) == (revision is None):
            raise SystemFailure('请选择一个发布版本或已保存草稿修订号。')
        if version is not None:
            document = self.version(system_id, version)
        else:
            system = self.get(system_id)
            if system['draft_revision'] != revision:
                raise SystemFailure('草稿已更新，请重新读取后试运行。', 409)
            document = system['draft']
        self.validate(document)
        snapshot = deepcopy(document['snapshot'])
        snapshot['system_ref'] = {'system_id': system_id, 'name': document['name'],
                                  'kind': 'published' if version else 'draft',
                                  'system_version': version, 'draft_revision': revision if revision else document['draft_revision']}
        return snapshot
