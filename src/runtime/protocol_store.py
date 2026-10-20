"""Append-only saved protocol revisions and releases in a separate catalog."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from src.runtime.protocols import parse_protocol
from src.runtime.system_store import SystemFailure


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class ProtocolStore:
    def __init__(self, root=None):
        self.root = Path(root or os.getenv('MAQA_PROTOCOLS_DIR') or Path(__file__).resolve().parents[2] / 'data/protocols')
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'protocols.sqlite3'
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS protocols (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS revisions (id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL, PRIMARY KEY(id,revision));
                CREATE TABLE IF NOT EXISTS releases (id TEXT NOT NULL, version INTEGER NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY(id,version), UNIQUE(id,revision));
            ''')

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.database, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            with db: yield db

    def list(self):
        with self.connection() as db: ids = [r[0] for r in db.execute('SELECT id FROM protocols ORDER BY rowid DESC')]
        return [self.get(key) for key in ids]

    def get(self, key):
        with self.connection() as db:
            row = db.execute('SELECT * FROM protocols WHERE id=?', (key,)).fetchone()
            if row is None: raise SystemFailure('协议不存在。', 404)
            draft = json.loads(db.execute('SELECT document FROM revisions WHERE id=? AND revision=?', (key, row['revision'])).fetchone()[0])
            releases = [json.loads(r[0]) for r in db.execute('SELECT document FROM releases WHERE id=? ORDER BY version DESC', (key,))]
        return dict(protocol_id=key, draft_revision=row['revision'], archived=bool(row['archived']), draft=draft, versions=releases)

    def save(self, fields, key=None, expected_revision=None):
        try: spec = parse_protocol(fields['spec']).model_dump()
        except ValueError as exc: raise SystemFailure(str(exc)) from exc
        name = fields['name'].strip()
        if not name or len(name) > 100: raise SystemFailure('协议名称需为 1 至 100 个字符。')
        key = key or 'protocol_' + uuid4().hex
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM protocols WHERE id=?', (key,)).fetchone()
            if (old is None and expected_revision is not None) or (old and old['revision'] != expected_revision):
                raise SystemFailure('协议草稿已更新，请重新读取。', 409)
            if old and old['archived']: raise SystemFailure('协议已归档，请复制后编辑。')
            revision = old['revision'] + 1 if old else 1
            document = dict(protocol_id=key, revision=revision, name=name, description=fields.get('description', ''),
                spec=spec, spec_sha256=digest(spec), created_at=datetime.now(timezone.utc).isoformat())
            db.execute('INSERT INTO revisions VALUES (?,?,?)', (key, revision, json.dumps(document, ensure_ascii=False)))
            if old: db.execute('UPDATE protocols SET revision=? WHERE id=?', (revision, key))
            else: db.execute('INSERT INTO protocols VALUES (?,?,0)', (key, revision))
        return self.get(key)

    def resolve(self, key, *, version=None, revision=None):
        if (version is None) == (revision is None): raise SystemFailure('请选择协议发布版本或已保存修订。')
        with self.connection() as db:
            if version is not None:
                import re
                if not re.fullmatch(r'v[1-9][0-9]*', version): raise SystemFailure('协议版本格式无效。')
                row = db.execute('SELECT document FROM releases WHERE id=? AND version=?', (key, int(version[1:]))).fetchone()
            else:
                row = db.execute('SELECT document FROM revisions WHERE id=? AND revision=?', (key, revision)).fetchone()
        if row is None: raise SystemFailure('协议版本不存在。', 404)
        doc = json.loads(row[0])
        if digest(doc['spec']) != doc['spec_sha256']: raise SystemFailure('协议内容校验失败。')
        return doc

    def publish(self, key, expected_revision):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM protocols WHERE id=?', (key,)).fetchone()
            if row is None: raise SystemFailure('协议不存在。', 404)
            if row['revision'] != expected_revision: raise SystemFailure('协议草稿已更新，请重新读取。', 409)
            if row['archived']: raise SystemFailure('协议已归档。')
            existing = db.execute('SELECT document FROM releases WHERE id=? AND revision=?', (key, expected_revision)).fetchone()
            if existing: return json.loads(existing[0])
            document = json.loads(db.execute('SELECT document FROM revisions WHERE id=? AND revision=?', (key, expected_revision)).fetchone()[0])
            parse_protocol(document['spec'])
            number = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM releases WHERE id=?', (key,)).fetchone()[0]
            document['version'] = f'v{number}'
            db.execute('INSERT INTO releases VALUES (?,?,?,?)', (key, number, expected_revision, json.dumps(document, ensure_ascii=False)))
        return document

    def archive(self, key, expected_revision):
        with self.connection() as db:
            cursor = db.execute('UPDATE protocols SET archived=1 WHERE id=? AND revision=?', (key, expected_revision))
            if cursor.rowcount != 1: raise SystemFailure('协议不存在或修订已变化。', 409)
        return self.get(key)
