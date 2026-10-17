"""Cross-database history behavior, without model calls or archive writes."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from src.runtime.models import RunContext, RunError, RunMetrics, RunRecord, TraceEvent
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository


def seed(repository, run_id, *, experiment=None, sample=None, failed=False, minute=0):
    context = RunContext(run_id=run_id, entrypoint='batch' if experiment else 'api',
        question=f'Question {run_id}', workflow_id='multi_hop_qa_v0',
        method_id='multi_agent_adaptive_v0' if experiment == 'v0' else 'multi_agent_adaptive_v1',
        model_name='offline-model', protocol_id='multi_hop_qa_v0', code_version='test',
        experiment_id=experiment, sample_id=sample)
    started = datetime(2026, 10, 17, 12, tzinfo=timezone.utc) + timedelta(minutes=minute)
    repository.create_run(RunRecord(context=context, status='running', started_at=started))
    repository.append_event(TraceEvent(run_id=run_id, sequence=0, event_type='run_started', timestamp=started))
    repository.append_event(TraceEvent(run_id=run_id, sequence=1,
        event_type='run_failed' if failed else 'run_completed', timestamp=started + timedelta(seconds=2),
        payload={'message': 'Preserved failure'} if failed else {'result': {'answer': 'Answer'}}))
    repository.update_run(RunRecord(context=context, status='failed' if failed else 'completed',
        started_at=started, finished_at=started + timedelta(seconds=2),
        final_state={'answer': 'Answer', 'verified': not failed,
            'supporting_evidence': [{'sq_id': 'sq_1', 'source_title': 'Source', 'source_paragraph': 0, 'text': 'Original evidence'}]},
        metrics=RunMetrics(duration_ms=2000, llm_call_count=2, total_tokens=100),
        error=RunError(error_type='execution_error', message='Preserved failure') if failed else None))


@pytest.fixture
def archive_client(tmp_path, monkeypatch):
    root = tmp_path / 'experiments'
    root.mkdir()
    catalog = tmp_path / 'catalog.json'
    specs = []
    for label in ('v0', 'v1'):
        repository = SQLiteRunRepository(root / f'{label}.sqlite3')
        repository.initialize()
        for i in range(10):
            seed(repository, f'{label}-{i}', experiment=label, sample=f'sample-{i}',
                 failed=label == 'v0' and i == 0, minute=i + (10 if label == 'v1' else 0))
        specs.append({'experiment_id': label, 'name': f'Smoke-10 {label.upper()}', 'database': f'{label}.sqlite3'})
    # Unregistered trial data must never appear.
    trial = SQLiteRunRepository(root / 'trial.sqlite3')
    trial.initialize()
    seed(trial, 'trial-0', experiment='trial')
    catalog.write_text(json.dumps({'experiments': specs}), encoding='utf-8')
    monkeypatch.setenv('MAQA_EXPERIMENT_CATALOG_PATH', str(catalog))
    monkeypatch.setenv('MAQA_EXPERIMENTS_DIR', str(root))
    primary = SQLiteRunRepository(tmp_path / 'daily.sqlite3')
    service = RunService(primary)
    seed(primary, 'daily', minute=30)
    original = app.state.run_service_factory
    app.state.run_service_factory = lambda: service
    try:
        with TestClient(app) as client:
            yield client, root, catalog, primary
    finally:
        app.state.run_service_factory = original


def test_twenty_archived_runs_filters_stats_details_and_export_are_consistent(archive_client):
    client, root, _catalog, primary = archive_client
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
    assert len(client.get('/api/v1/experiments').json()) == 2
    listing = client.get('/api/v1/runs', params={'entrypoint': 'batch', 'limit': 10}).json()
    assert listing['total'] == 20
    assert [item['context']['run_id'] for item in listing['items']] == [f'v1-{i}' for i in reversed(range(10))]
    second = client.get('/api/v1/runs', params={'entrypoint': 'batch', 'limit': 10, 'offset': 10}).json()
    assert [item['context']['run_id'] for item in second['items']] == [f'v0-{i}' for i in reversed(range(10))]
    summary = client.get('/api/v1/stats/summary', params={'entrypoint': 'batch'}).json()
    assert (summary['total_runs'], summary['completed_runs'], summary['failed_runs']) == (20, 19, 1)
    assert summary['completion_rate'] == 0.95
    for experiment in ('v0', 'v1'):
        query = {'entrypoint': 'batch', 'experiment_id': experiment}
        assert client.get('/api/v1/runs', params=query).json()['total'] == 10
        assert client.get('/api/v1/stats/summary', params=query).json()['total_runs'] == 10
        exported = client.get('/api/v1/exports/runs.jsonl', params=query)
        rows = [json.loads(line) for line in exported.text.splitlines()]
        assert len(rows) == 10 and all(row['run']['context']['experiment_id'] == experiment for row in rows)
    failed = client.get('/api/v1/runs/v0-0', params={'experiment_id': 'v0'}).json()
    assert failed['run']['status'] == 'failed' and failed['run']['error']['message'] == 'Preserved failure'
    assert failed['run']['context']['sample_id'] == 'sample-0'
    assert failed['run']['metrics']['total_tokens'] == 100
    assert failed['run']['final_state']['supporting_evidence'][0]['text'] == 'Original evidence'
    assert failed['events'] == client.get('/api/v1/runs/v0-0/events').json()
    assert failed['events'][-1]['event_type'] == 'run_failed'
    assert client.get('/api/v1/runs/v0-0', params={'experiment_id': 'v1'}).status_code == 404
    assert client.get('/api/v1/runs/trial-0').status_code == 404
    assert client.get('/api/v1/runs', params={'entrypoint': 'api'}).json()['total'] == 1
    assert client.get('/api/v1/runs').json()['total'] == 21
    assert primary.count_runs() == 1
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}


def test_status_method_time_and_experiment_filters_apply_to_every_endpoint(archive_client):
    client, *_ = archive_client
    filters = {'entrypoint': 'batch', 'experiment_id': 'v0', 'status': 'failed',
               'method_id': 'multi_agent_adaptive_v0', 'started_from': '2026-10-17T12:00:00Z'}
    assert client.get('/api/v1/runs', params=filters).json()['total'] == 1
    assert client.get('/api/v1/stats/summary', params=filters).json()['total_runs'] == 1
    assert len(client.get('/api/v1/exports/runs.jsonl', params=filters).text.splitlines()) == 1
    filters['started_from'] = '2026-10-17T12:01:00Z'
    assert client.get('/api/v1/runs', params=filters).json()['total'] == 0
    assert client.get('/api/v1/stats/summary', params=filters).json()['total_runs'] == 0
    assert client.get('/api/v1/exports/runs.jsonl', params=filters).text == ''


def test_missing_archives_do_not_create_files_or_break_daily_history(archive_client):
    client, root, catalog, _ = archive_client
    catalog.write_text(json.dumps({'experiments': [{'experiment_id': 'absent', 'name': 'Missing', 'database': 'missing.sqlite3'}]}), encoding='utf-8')
    assert client.get('/api/v1/experiments').json() == []
    assert client.get('/api/v1/runs').json()['total'] == 1
    assert not (root / 'missing.sqlite3').exists()


@pytest.mark.parametrize('database', ['../daily.sqlite3', '/outside.sqlite3'])
def test_archive_registry_cannot_read_outside_experiments_directory(archive_client, database):
    client, _, catalog, _ = archive_client
    catalog.write_text(json.dumps({'experiments': [{'experiment_id': 'bad', 'name': 'Invalid', 'database': database}]}), encoding='utf-8')
    response = client.get('/api/v1/runs')
    assert response.status_code == 503
    assert database not in response.text


def test_archive_identity_mismatch_is_not_silently_mislabeled(archive_client):
    client, _, catalog, _ = archive_client
    catalog.write_text(json.dumps({'experiments': [{'experiment_id': 'wrong', 'name': 'Wrong', 'database': 'v0.sqlite3'}]}), encoding='utf-8')
    assert client.get('/api/v1/runs', params={'entrypoint': 'batch'}).status_code == 503
