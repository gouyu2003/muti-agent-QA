import json
import subprocess
from time import monotonic, sleep
import pytest
from fastapi.testclient import TestClient

from backend.app import app, build_api_run_context
from src.runtime.models import RunRecord, RunMetrics, RunError
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository


@pytest.fixture
def api(tmp_path, monkeypatch):
    for key, folder in [('MAQA_KNOWLEDGE_DIR', 'knowledge'), ('MAQA_SYSTEMS_DIR', 'systems'), ('MAQA_PROTOCOLS_DIR', 'protocols'), ('MAQA_EVALUATIONS_DIR', 'evaluations')]:
        monkeypatch.setenv(key, str(tmp_path / folder))
    monkeypatch.setenv('MAQA_EXPERIMENT_CATALOG_PATH', str(tmp_path / 'absent.json'))
    repository = SQLiteRunRepository(tmp_path / 'runs.sqlite3')
    class Graph:
        def stream(self, *args, **kwargs):
            yield {'type': 'updates', 'data': {'answer_validator_v0': {'answer': 'fake', 'verified': True}}}
    service = RunService(repository, graph_factory=lambda usage: Graph())
    monkeypatch.setattr(app.state, 'run_service_factory', lambda: service)
    # If test execution ever escapes to production spawning, fail before any process can start.
    original_popen = subprocess.Popen
    def forbid_worker(command, *args, **kwargs):
        if 'src.runtime.interactive_worker' in command or 'src.interactive_bootstrap' in command:
            raise AssertionError('Test attempted a detached production worker')
        return original_popen(command, *args, **kwargs)
    monkeypatch.setattr('src.runtime.interactive.subprocess.Popen', forbid_worker)
    with TestClient(app) as client:
        yield client, repository


def test_submit_status_cancel_and_event_replay_are_compatible(api):
    client, repository = api
    body = {'question': 'Q', 'client_request_id': 'same-submit', 'workflow_id': 'multi_hop_qa_fixed'}
    submitted = client.post('/api/v1/qa/runs', json=body)
    assert submitted.status_code == 202
    run_id = submitted.json()['run_id']
    assert client.post('/api/v1/qa/runs', json=body).json()['run_id'] == run_id
    deadline = monotonic()+5
    while client.get(f'/api/v1/runs/{run_id}/status').json()['status'] in ('running', 'queued'):
        assert monotonic() < deadline
        sleep(.03)
    assert client.post(f'/api/v1/runs/{run_id}/cancel').json()['status'] == 'completed'
    assert isinstance(client.get(f'/api/v1/runs/{run_id}/events').json(), list)
    stream = client.get(f'/api/v1/runs/{run_id}/events/stream?after_sequence=0').text
    assert 'event: run_completed' in stream and 'event: run_started' not in stream
    assert repository.count_runs() == 1
    assert client.post('/api/v1/qa/runs', json={**body, 'question': 'changed'}).status_code == 409


def test_chinese_search_is_versioned_and_creates_no_qa_runs(api):
    client, repository = api
    source = client.post('/api/v1/knowledge/sources', data={'name': '中文'},
        files=[('files', ('规定.md', '# 报销\n\n报销需要提交电子发票。'.encode(), 'text/markdown'))]).json()
    path = f"/api/v1/knowledge/sources/{source['source_id']}/versions/1/search"
    found = client.post(path, json={'query': '报销发票', 'top_k': 5})
    assert found.status_code == 200
    assert found.json()['index_version'] == 'bm25-zh-mixed-v1'
    assert found.json()['items'][0]['line_start'] == 3
    assert repository.count_runs() == 0
    assert client.post(path, json={'query': '  '}).status_code == 422
    assert client.post(path, json={'query': '发票', 'top_k': 0}).status_code == 422


def test_diagnostic_filter_matches_list_statistics_and_export(api):
    from datetime import datetime, timezone
    client, repository = api
    for run_id, code in [('bad', 'revision_missing_citation'), ('other', 'invalid_json')]:
        now = datetime.now(timezone.utc)
        record = RunRecord(context=build_api_run_context('Q', run_id=run_id), status='running', started_at=now)
        repository.create_run(record)
        repository.update_run(record.model_copy(update={'status': 'failed', 'finished_at': now,
            'error': RunError(error_type='output_error', message='校验失败'),
            'metrics': RunMetrics(agent_usage={'a': {'calls': [{'outcome': 'invalid_schema', 'validation_errors': [{'code': code, 'path': 'supporting_evidence', 'message': 'missing'}]}]}})}))
    query = '?diagnostic=revision_missing_citation'
    assert client.get('/api/v1/runs'+query).json()['total'] == 1
    assert client.get('/api/v1/stats/summary'+query).json()['total_runs'] == 1
    lines = client.get('/api/v1/exports/runs.jsonl'+query).text.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])['run']['context']['run_id'] == 'bad'
