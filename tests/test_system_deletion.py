"""Deleting a composition must preserve independent histories and shared sources."""
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from src.runtime.context import build_v0_run_context
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.system_store import SystemFailure, SystemStore
from test_system_management import stores, fields, FakeGraph


def test_delete_removes_draft_and_releases_but_keeps_history_and_shared_material(stores, tmp_path):
    systems, knowledge = stores
    source = knowledge.import_files('owner', 'Shared material', [('notes.txt', b'Cedar Institute is in Northbridge.')])
    binding = {'source_id': source['source_id'], 'source_version': 1}
    target = systems.create(fields('Delete me', **binding))
    retained = systems.create(fields('Keep me', **binding))
    key = target['system_id']
    systems.publish(key, 1)
    context = build_v0_run_context('Where?', entrypoint='api', system_id=key, system_version='v1')
    repository = SQLiteRunRepository(tmp_path / 'runs.sqlite3')
    before = RunService(repository, graph_factory=lambda _: FakeGraph()).run(context)
    systems.save(key, 1, fields('Second release', **binding))
    systems.publish(key, 2)

    with pytest.raises(SystemFailure) as stale:
        systems.delete(key, 1)
    assert stale.value.status == 409
    assert len(systems.get(key)['versions']) == 2

    systems.delete(key, 2)
    restarted = SystemStore()
    assert [s['system_id'] for s in restarted.list()] == [retained['system_id']]
    for lookup in (lambda: restarted.get(key), lambda: restarted.version(key, 'v1'),
                   lambda: restarted.run_snapshot(key, version='v2')):
        with pytest.raises(SystemFailure) as missing:
            lookup()
        assert missing.value.status == 404
    assert repository.get_run_detail(context.run_id) == before
    assert source['source_id'] in [s['source_id'] for s in knowledge.list_sources('owner')]
    assert restarted.get(retained['system_id']) == retained


def test_delete_api_requires_revision_and_returns_conflicts_and_missing(stores, tmp_path, monkeypatch):
    monkeypatch.setenv('MAQA_RUN_DB_PATH', str(tmp_path / 'runs.sqlite3'))
    with TestClient(app) as client:
        item = client.post('/api/v1/systems', json=fields()).json()
        key = item['system_id']
        assert client.post(f'/api/v1/systems/{key}/versions', json={'expected_revision': 1}).status_code == 200
        assert client.delete(f'/api/v1/systems/{key}').status_code == 422
        assert client.delete(f'/api/v1/systems/{key}?expected_revision=0').status_code == 422
        assert client.delete(f'/api/v1/systems/{key}?expected_revision=2').status_code == 409
        assert client.get(f'/api/v1/systems/{key}').status_code == 200
        deleted = client.delete(f'/api/v1/systems/{key}?expected_revision=1')
        assert deleted.status_code == 200
        assert deleted.json() == {'deleted': True, 'system_id': key}
        assert client.get(f'/api/v1/systems/{key}').status_code == 404
        assert client.get(f'/api/v1/systems/{key}/versions/v1').status_code == 404
        assert client.delete(f'/api/v1/systems/{key}?expected_revision=1').status_code == 404
        assert client.get('/api/v1/systems').json()['items'] == []
