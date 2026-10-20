"""Saved systems, workspace migration and execution provenance (no model calls)."""
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from src.knowledge.store import KnowledgeStore
from src.runtime.context import build_v0_run_context
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.system import retriever_from_snapshot
from src.runtime.system_store import SystemFailure, SystemStore
from main import parse_args, run as cli_run


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv('MAQA_KNOWLEDGE_DIR', str(tmp_path / 'knowledge'))
    monkeypatch.setenv('MAQA_SYSTEMS_DIR', str(tmp_path / 'systems'))
    monkeypatch.setenv('MAQA_EXPERIMENT_CATALOG_PATH', str(tmp_path / 'missing.json'))
    return SystemStore(), KnowledgeStore()


def fields(name='Research system', **overrides):
    return {'name': name, 'description': 'Saved composition', 'workflow_id': 'multi_hop_qa_v0',
            'source_id': 'hotpotqa_fixture', 'source_version': None, **overrides}


def test_two_systems_survive_restart_and_share_pinned_material(stores):
    systems, knowledge = stores
    source = knowledge.import_files('legacy-owner', 'Shared notes', [('notes.txt', b'Cedar Institute is in Northbridge.')])
    binding = {'source_id': source['source_id'], 'source_version': 1}
    a = systems.create(fields('A', **binding))
    b = systems.create(fields('B', **binding))
    systems.publish(a['system_id'], 1)
    systems.publish(b['system_id'], 1)
    knowledge.import_files('another-system', None, [('notes.txt', b'Cedar Institute moved to Southbridge.')], source['source_id'])
    restarted = SystemStore()
    assert len(restarted.list()) == 2
    for item in (a, b):
        context = build_v0_run_context('Where is Cedar Institute?', entrypoint='cli', system_id=item['system_id'], system_version='v1')
        assert context.config_snapshot['source']['version'] == 1
        chunks = retriever_from_snapshot(context.config_snapshot).search('Cedar Institute')
        assert 'Northbridge' in chunks[0]['text'] and 'Southbridge' not in chunks[0]['text']


def test_draft_revision_publish_idempotence_and_template_freezing(stores, monkeypatch):
    systems, _ = stores
    item = systems.create(fields())
    key = item['system_id']
    original = deepcopy(item['draft']['snapshot'])
    release = systems.publish(key, 1)
    assert systems.publish(key, 1) == release
    monkeypatch.setattr('src.runtime.system_store.resolve_system', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not read templates')))
    saved = systems.save(key, 1, fields('Renamed draft'))
    assert saved['draft_revision'] == 2
    assert saved['draft']['snapshot'] == original
    assert systems.version(key, 'v1') == release
    assert systems.publish(key, 2)['version'] == 'v2'
    assert systems.run_snapshot(key, 'v1')['system_ref']['name'] == 'Research system'
    with pytest.raises(SystemFailure) as error:
        systems.save(key, 1, fields())
    assert error.value.status == 409
    with pytest.raises(SystemFailure) as error:
        systems.run_snapshot(key, revision=1)
    assert error.value.status == 409
    assert systems.run_snapshot(key, revision=2)['system_ref']['kind'] == 'draft'


def test_incomplete_draft_and_missing_or_changed_source_never_fall_back(stores):
    systems, knowledge = stores
    draft = systems.create(fields(source_id=None, workflow_id=None))
    with pytest.raises(SystemFailure, match='请先选择'):
        systems.publish(draft['system_id'], 1)
    source = knowledge.import_files('old', 'Notes', [('notes.txt', b'Cedar Institute is in Northbridge.')])
    incomplete = systems.create(fields(source_id=source['source_id']))
    with pytest.raises(SystemFailure, match='明确'):
        systems.publish(incomplete['system_id'], 1)
    item = systems.create(fields(source_id=source['source_id'], source_version=1))
    release = systems.publish(item['system_id'], 1)
    path = __import__('pathlib').Path(release['snapshot']['source']['resolved_path'])
    path.write_text('{}')
    with pytest.raises(SystemFailure, match='校验失败'):
        systems.run_snapshot(item['system_id'], 'v1')
    path.unlink()
    with pytest.raises(SystemFailure):
        systems.run_snapshot(item['system_id'], 'v1')
    with pytest.raises(SystemFailure) as error:
        systems.version(item['system_id'], 'v99')
    assert error.value.status == 404


def test_v1_knowledge_migration_backup_idempotence_and_shared_ownership(tmp_path):
    root = tmp_path / 'knowledge'
    root.mkdir()
    database = root / 'knowledge.sqlite3'
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE sources (source_id TEXT PRIMARY KEY, system_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL); INSERT INTO sources VALUES ('old', 'owner', 'Old notes', 'today'); PRAGMA user_version=1;")
    store = KnowledgeStore(root)
    assert store.list_sources('new-system')[0]['source_id'] == 'old'
    backups = list(root.glob('knowledge.pre-workspace-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert 'workspace_id' not in {r[1] for r in db.execute('PRAGMA table_info(sources)')}
    KnowledgeStore(root)
    assert list(root.glob('knowledge.pre-workspace-*.sqlite3')) == backups
    with store.connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3


def test_migration_rolls_back_added_column_on_failure(tmp_path, monkeypatch):
    root = tmp_path / 'knowledge'
    root.mkdir()
    database = root / 'knowledge.sqlite3'
    with sqlite3.connect(database) as db:
        db.executescript('CREATE TABLE sources (source_id TEXT PRIMARY KEY, system_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL); PRAGMA user_version=1;')

    @contextmanager
    def denied(self):
        with sqlite3.connect(self.database) as db:
            db.set_authorizer(lambda action, arg1, arg2, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and arg1 == 'user_version' and arg2 else sqlite3.SQLITE_OK)
            yield db
    monkeypatch.setattr(KnowledgeStore, 'connection', denied)
    with pytest.raises(sqlite3.DatabaseError):
        KnowledgeStore(root)
    with sqlite3.connect(database) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert 'workspace_id' not in {r[1] for r in db.execute('PRAGMA table_info(sources)')}
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='versions'").fetchone() is None


class FakeGraph:
    def __init__(self, fail=False):
        self.fail = fail

    def stream(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError('offline failure')
        yield {'type': 'updates', 'data': {'answer_validator_v0': {'answer': 'Answer', 'verified': True}}}


def test_cli_uses_explicit_system_versions_and_rejects_overrides(stores, tmp_path):
    systems, _ = stores
    item = systems.create(fields())
    key = item['system_id']
    systems.publish(key, 1)
    args = parse_args(['Question?', '--system-id', key, '--system-version', 'v1'])
    service = RunService(SQLiteRunRepository(tmp_path / 'cli.sqlite3'), graph_factory=lambda _: FakeGraph())
    detail = cli_run('Question?', system_id=args.system_id, system_version=args.system_version, run_service=service)
    assert detail.run.context.config_snapshot['system_ref']['system_version'] == 'v1'
    for overrides in ({'source_id': 'demo_fixture_002'}, {'workflow_id': 'multi_hop_qa_fixed'}, {'catalog_path': 'anything.json'}):
        with pytest.raises(ValueError):
            cli_run('Q', system_id=key, system_version='v1', run_service=service, **overrides)


def test_run_lifecycle_keeps_frozen_system_reference(stores, tmp_path):
    systems, _ = stores
    item = systems.create(fields())
    key = item['system_id']
    systems.publish(key, 1)
    repository = SQLiteRunRepository(tmp_path / 'runs.sqlite3')
    for outcome in ('completed', 'failed', 'cancelled'):
        context = build_v0_run_context('Question?', entrypoint='cli', run_id=outcome, system_id=key, draft_revision=1)
        service = RunService(repository, graph_factory=lambda _: FakeGraph(outcome == 'failed'))
        if outcome == 'cancelled':
            iterator = service.stream(context)
            next(iterator)  # started
            next(iterator)  # enter execution before cancellation
            iterator.close()
        else:
            service.run(context)
        detail = repository.get_run_detail(outcome)
        assert detail.run.status == outcome
        assert detail.run.context.config_snapshot['system_ref']['draft_revision'] == 1
    systems.save(key, 1, fields('Changed'))
    assert repository.get_run_detail('completed').run.context.config_snapshot['system_ref']['name'] == 'Research system'


def test_management_api_sse_filters_and_legacy_compatibility(stores, tmp_path):
    original = app.state.run_service_factory
    app.state.run_service_factory = lambda: RunService(SQLiteRunRepository(tmp_path / 'runs.sqlite3'), graph_factory=lambda _: FakeGraph())
    try:
        with TestClient(app) as client:
            item = client.post('/api/v1/systems', json=fields()).json()
            key = item['system_id']
            second = client.post('/api/v1/systems', json=fields('Second')).json()['system_id']
            assert client.post(f'/api/v1/systems/{key}/versions', json={'expected_revision': 1}).json()['version'] == 'v1'
            assert client.get(f'/api/v1/systems/{key}/versions/v1').json()['name'] == 'Research system'
            assert client.put(f'/api/v1/systems/{key}/draft', json={**fields('New name'), 'expected_revision': 1}).status_code == 200
            assert client.post(f'/api/v1/systems/{key}/versions', json={'expected_revision': 1}).status_code == 409
            assert client.post('/api/v1/qa/stream', json={'question': 'Q', 'system_id': key, 'draft_revision': 1}).status_code == 409
            invalid = [
                {'system_id': key}, {'system_version': 'v1'},
                {'system_id': key, 'system_version': 'v1', 'draft_revision': 2},
                {'system_id': key, 'system_version': 'v1', 'source_id': 'hotpotqa_fixture'},
            ]
            for selection in invalid:
                assert client.post('/api/v1/qa/stream', json={'question': 'Q', **selection}).status_code == 422
            for selection in ({'system_id': key, 'system_version': 'v1'}, {'system_id': key, 'draft_revision': 2}, {'system_id': second, 'draft_revision': 1}, {}):
                response = client.post('/api/v1/qa/stream', json={'question': 'Q', **selection})
                assert response.status_code == 200 and 'event: run_completed' in response.text
            query = {'system_id': key}
            listing = client.get('/api/v1/runs', params=query).json()
            assert listing['total'] == 2
            assert client.get('/api/v1/stats/summary', params=query).json()['total_runs'] == 2
            exported = [json.loads(line) for line in client.get('/api/v1/exports/runs.jsonl', params=query).text.splitlines()]
            assert len(exported) == 2
            assert {r['run']['context']['config_snapshot']['system_ref']['name'] for r in exported} == {'Research system', 'New name'}
            assert client.get('/api/v1/runs').json()['total'] == 4
            assert client.get('/api/v1/systems/unknown').status_code == 404
            assert client.post('/api/v1/systems', json=fields(workflow_id='unknown')).status_code == 422
            assert client.post('/api/v1/systems', json={**fields(), 'snapshot': {}}).status_code == 422
    finally:
        app.state.run_service_factory = original
