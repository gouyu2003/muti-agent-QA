from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from backend.app import app
from src.runtime.protocols import ProtocolSpec, parse_protocol, compile_protocol, protocol_templates
from src.runtime.protocol_store import ProtocolStore
from src.runtime.system_store import SystemFailure


def test_saved_revisions_releases_and_archive_are_immutable(tmp_path):
    store = ProtocolStore(tmp_path)
    fields = dict(name='Custom pipeline', spec=protocol_templates()[0]['spec'])
    item = store.save(fields)
    key = item['protocol_id']
    v1 = store.publish(key, 1)
    fields['spec']['stages'][1]['max_visits'] = 3
    store.save(fields, key, 1)
    assert store.resolve(key, revision=1)['spec'] == v1['spec']
    assert store.resolve(key, version='v1') == v1
    with pytest.raises(SystemFailure): store.save(fields, key, 1)
    assert store.publish(key, 2)['version'] == 'v2'
    store.archive(key, 2)
    with pytest.raises(SystemFailure): store.save(fields, key, 2)
    assert store.resolve(key, version='v1') == v1


@pytest.mark.parametrize('change', ['code', 'expression', 'unreachable', 'bad_role', 'unbounded', 'nested', 'no_end'])
def test_protocol_rejects_unregistered_rules(change):
    raw = deepcopy(protocol_templates()[0]['spec'])
    if change == 'code': raw['code'] = 'print(1)'
    if change == 'expression': raw['stages'][-1]['routes'][0]['condition'] = 'eval(question)'
    if change == 'unreachable': raw['stages'][0]['routes'][0]['target'] = 'review'
    if change == 'bad_role': raw['stages'][0]['slot'] = 'reviewer'
    if change == 'unbounded': raw['stages'][0]['max_visits'] = 0
    if change == 'nested': raw['stages'][0]['children'] = []
    if change == 'no_end': raw['stages'][-1]['routes'][0]['target'] = 'plan'
    with pytest.raises(ValueError): parse_protocol(raw)


def test_protocol_management_http(tmp_path, monkeypatch):
    monkeypatch.setenv('MAQA_PROTOCOLS_DIR', str(tmp_path))
    monkeypatch.setenv('MAQA_SYSTEMS_DIR', str(tmp_path / 'systems'))
    monkeypatch.setenv('MAQA_KNOWLEDGE_DIR', str(tmp_path / 'knowledge'))
    monkeypatch.setenv('MAQA_RUN_DB_PATH', str(tmp_path / 'runs.sqlite3'))
    with TestClient(app, raise_server_exceptions=True) as client:
        catalog = client.get('/api/v1/protocols/templates').json()
        from pathlib import Path
        import json
        assert catalog == json.loads((Path(__file__).resolve().parents[1] / 'frontend/src/protocolCatalog.fixture.json').read_text(encoding='utf-8'))
        templates = catalog['items']
        assert len(templates) == 6 and set(catalog['capabilities']) == {'1', '2'}
        fields = dict(name='Saved from browser', spec=templates[0]['spec'])
        response = client.post('/api/v1/protocols', json=fields)
        assert response.status_code == 201
        key = response.json()['protocol_id']
        assert client.post(f'/api/v1/protocols/{key}/versions', json={'expected_revision': 1}).status_code == 200
        assert client.get(f'/api/v1/protocols/{key}/versions/v1').json()['spec'] == fields['spec']
        assert client.put(f'/api/v1/protocols/{key}/draft', json={**fields, 'expected_revision': 2}).status_code == 409
        assert client.post('/api/v1/protocols/validate', json={**fields['spec'], 'code': 'x'}).status_code == 422
