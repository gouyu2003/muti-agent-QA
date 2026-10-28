"""Exercise platform jobs with real local graphs, isolated stores and forbidden networking."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from backend.app import app
from src.evaluation.experiment_config import ExperimentConfig, METHODS
from src.evaluation.experiment_methods import method_definition
from src.evaluation.experiment_store import Ledger, experiment_lock
from src.evaluation.platform_store import EvaluationFailure, EvaluationStore, JobRequest
from src.evaluation.platform_worker import execute, reconcile, start_job, stop_job, exported_csv
from src.runtime.protocol_store import ProtocolStore
from src.runtime.service import RunService
from src.runtime.system_store import SystemStore


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    # Keep Windows paths below MAX_PATH, including the existing atomic context suffix.
    tmp_path = tmp_path_factory.mktemp('p')
    for env, folder in [('MAQA_EVALUATIONS_DIR', 'evaluations'), ('MAQA_SYSTEMS_DIR', 'systems'),
                        ('MAQA_KNOWLEDGE_DIR', 'knowledge'), ('MAQA_PROTOCOLS_DIR', 'protocols')]:
        monkeypatch.setenv(env, str(tmp_path / folder))
    monkeypatch.setenv('MAQA_RUN_DB_PATH', str(tmp_path / 'daily.sqlite3'))
    monkeypatch.setenv('MAQA_EXPERIMENT_CATALOG_PATH', str(tmp_path / 'missing.json'))
    def forbidden(*args, **kwargs):
        raise AssertionError('Real model calls are forbidden in platform tests')
    monkeypatch.setattr('src.llm.build_chat_model', forbidden)
    import socket
    original_connect = socket.socket.connect
    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return original_connect(sock, address)
        return forbidden(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_only)


def questions(count=2, *, context=True, labelled=True):
    return [dict(id=f'q{i}', question=f'Where is Cedar Institute {i}?',
                 **({'answer': 'Northbridge'} if labelled else {}),
                 **({'context': [[f'Cedar {i}', [f'Cedar Institute {i} is in Northbridge.']]],
                     'supporting_facts': [[f'Cedar {i}', 0]], 'type': 'bridge'} if context else {})) for i in range(count)]


def new_job(store, *, methods=None, records=None, **overrides):
    bank = store.import_bank('My questions', '', records if records is not None else questions())
    return store.create_job(JobRequest(name='Comparison', bank_id=bank['bank_id'], bank_version=1,
        targets=[dict(kind='builtin', method=m) for m in (methods or ['single_rag'])], **overrides), SystemStore())


def fake_graph_phases(monkeypatch, *, fail=False):
    calls = []
    def phase(stage, agent_id, config, source, inputs, usage):
        from src.runtime.protocol_runtime import ProtocolExecutionError
        calls.append((stage.kind, deepcopy(inputs)))
        if fail:
            raise ProtocolExecutionError('Controlled protocol failure')
        if stage.kind == 'plan':
            return {'sub_questions': [dict(id='sq_1', question=inputs['question'], keywords=['Cedar'], depends_on=[])]}
        if stage.kind == 'lead':
            return dict(action='review', tasks=[], summary='Northbridge', reason='done') if inputs['reads'] else dict(
                action='dispatch', tasks=[dict(task_id='t1', agent_id='experts_0', instruction='Locate Cedar Institute')], reason='chosen')
        return dict(answer='Northbridge', verified=True, failure_type='none', supporting_evidence=[], evidence_chunks=[])
    monkeypatch.setattr('src.runtime.protocol_runtime.execute_phase', phase)
    return calls


def test_versioned_banks_validation_and_no_models():
    store = EvaluationStore()
    v1 = store.import_bank('Questions', '', questions())
    v2 = store.import_bank('Questions v2', '', questions(3), v1['bank_id'])
    assert v2['version'] == 2
    assert store.bank(v1['bank_id'], 1, public=True)['count'] == 2
    assert len(store.banks()) == 2
    broken = questions(); broken[0]['supporting_facts'][0][1] = 902
    with pytest.raises(EvaluationFailure, match='坐标'):
        store.import_bank('Bad', '', broken)
    with pytest.raises(EvaluationFailure, match='ID'):
        store.import_bank('Duplicate', '', questions(1) * 2)
    no_context = store.import_bank('Regression', '', questions(context=False, labelled=False))
    assert no_context['labelled'] == no_context['contextual'] == 0


def test_six_graphs_labels_and_storage_isolation(monkeypatch):
    store = EvaluationStore()
    records = questions(1)
    records[0]['answer'] = 'SECRET_GOLD_ONLY_FOR_SCORING'
    job = new_job(store, methods=METHODS, records=records)
    calls = fake_graph_phases(monkeypatch)
    store.queue(job['job_id'])
    execute(store, job['job_id'], service_factory=RunService)
    result = store.job(job['job_id'])
    assert result['status'] == 'completed' and result['report']['complete']
    assert result['progress']['completed'] == 6
    assert result['report']['retry_count'] == 0
    assert 'SECRET_GOLD_ONLY_FOR_SCORING' not in json.dumps(calls)
    for path in (store.job_directory(job['job_id']) / 'runs').glob('*.json'):
        assert 'SECRET_GOLD_ONLY_FOR_SCORING' not in path.read_text(encoding='utf-8')
    assert not Path(os.environ['MAQA_RUN_DB_PATH']).exists()
    assert not (Path(os.environ['MAQA_KNOWLEDGE_DIR']) / 'knowledge.sqlite3').exists()


def test_pause_resume_and_retry_keep_first_attempt(monkeypatch):
    store = EvaluationStore(); job = new_job(store)
    fake_graph_phases(monkeypatch)
    class PauseAfterFirst(RunService):
        def run(self, context):
            result = super().run(context)
            stop_job(store, job['job_id'])
            return result
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=PauseAfterFirst)
    assert store.job(job['job_id'])['status'] == 'paused'
    assert not store.job(job['job_id'])['report']['complete']
    calls = fake_graph_phases(monkeypatch, fail=True)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    assert len(calls) == 1
    assert store.job(job['job_id'])['progress']['failed'] == 1
    calls = fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    assert not calls  # Default resume cannot replay failures.
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService, retry_failed=True)
    report = store.job(job['job_id'])['report']
    assert report['retry_count'] == 1
    assert report['methods']['single_rag']['scores']['em']['value'] == .5
    assert report['latest_attempt_supplement']['single_rag']['scores']['em']['value'] == 1


def test_dead_worker_reconciles_persisted_terminal_without_duplicate(monkeypatch):
    store = EvaluationStore(); job = new_job(store)
    calls = fake_graph_phases(monkeypatch)
    class CrashAfterPersist(RunService):
        def run(self, context):
            super().run(context)
            raise KeyboardInterrupt
    store.queue(job['job_id'])
    with pytest.raises(KeyboardInterrupt): execute(store, job['job_id'], service_factory=CrashAfterPersist)
    reconcile(store, job['job_id'])
    assert store.job(job['job_id'])['status'] == 'interrupted'
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    assert len(calls) == 2 and store.job(job['job_id'])['progress']['completed'] == 2


def test_unknown_request_is_not_automatically_replayed(monkeypatch):
    store = EvaluationStore(); job = new_job(store)
    class Unknown(RunService):
        def run(self, context):
            raise KeyboardInterrupt
    store.queue(job['job_id'])
    with pytest.raises(KeyboardInterrupt): execute(store, job['job_id'], service_factory=Unknown)
    reconcile(store, job['job_id'])
    calls = fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    report = store.job(job['job_id'])['report']
    assert len(calls) == 1 and not report['complete']
    assert store.job(job['job_id'])['progress']['interrupted'] == 1


def test_worker_lock_single_job_and_explicit_launch(monkeypatch):
    store = EvaluationStore(); first = new_job(store); second = new_job(store)
    with pytest.raises(EvaluationFailure, match='明确确认'):
        start_job(store, first['job_id'], allow_model_calls=False)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-not-a-real-key')
    monkeypatch.setenv('DEEPSEEK_BASE_URL', 'https://example.invalid/v1')
    launches = []
    monkeypatch.setattr('src.evaluation.platform_worker.subprocess.Popen', lambda args, **kw: launches.append((args, kw)))
    start_job(store, first['job_id'], allow_model_calls=True)
    assert len(launches) == 1 and 'src.evaluation.platform_worker' in launches[0][0]
    assert launches[0][1].get('start_new_session') or launches[0][1].get('creationflags')
    with pytest.raises(EvaluationFailure, match='同一时间'):
        start_job(store, second['job_id'], allow_model_calls=True)
    store.state(first['job_id'], 'running')
    with experiment_lock(store.job_directory(first['job_id'])):
        reconcile(store, first['job_id'])
        assert store.job(first['job_id'])['status'] == 'running'
    reconcile(store, first['job_id'])
    assert store.job(first['job_id'])['status'] == 'interrupted'


def published_custom_system(source_id='hotpotqa_fixture', source_version=None):
    systems = SystemStore(); protocols = ProtocolStore()
    config = ExperimentConfig(dataset='unused', split_directory='unused', output='unused')
    definition = method_definition(config, 'pipeline_fixed')
    spec = definition['snapshot']['protocol_document']['spec']
    # A renamed stage is deliberately outside the six standard baseline shapes.
    old = spec['stages'][0]['stage_id']; renamed = 'my_custom_plan'
    spec['entry_stage'] = renamed
    for stage in spec['stages']:
        if stage['stage_id'] == old: stage['stage_id'] = renamed
        stage['reads'] = [renamed if x == old else x for x in stage['reads']]
        if stage['on_limit'] == old: stage['on_limit'] = renamed
        for route in stage['routes']:
            if route['target'] == old: route['target'] = renamed
    protocol = protocols.save(dict(name='Custom protocol', spec=spec))
    protocols.publish(protocol['protocol_id'], 1)
    instances = definition['snapshot']['instances']
    for a in instances:
        if a['agent_type'] in ('retriever', 'expert'): a.update(source_id=source_id, source_version=source_version)
    system = systems.create(dict(name='Custom QA', agents=instances, protocol_ref=dict(protocol_id=protocol['protocol_id'], version='v1'),
                                 protocol_bindings=definition['snapshot']['protocol_bindings']))
    systems.publish(system['system_id'], 1)
    return systems, system['system_id']


def test_custom_published_versions_and_own_materials(monkeypatch):
    systems, key = published_custom_system()
    before = systems.database.read_bytes()
    store = EvaluationStore(); bank = store.import_bank('Regression', '', questions(context=False))
    job = store.create_job(JobRequest(name='Custom comparison', bank_id=bank['bank_id'], bank_version=1,
        targets=[dict(kind='system', system_id=key, version='v1')], context_mode='system_sources'), systems)
    assert systems.database.read_bytes() == before
    calls = fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    assert len(calls) == 6 and store.job(job['job_id'])['report']['complete']
    assert systems.database.read_bytes() == before
    assert next(iter(store.frozen(job['job_id'])[1]['definitions'].values()))['snapshot']['system_ref']['system_version'] == 'v1'


def test_no_answer_labels_remain_unscored(monkeypatch):
    store = EvaluationStore(); job = new_job(store, records=questions(labelled=False))
    fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    method = store.job(job['job_id'])['report']['methods']['single_rag']
    assert method['complete'] and method['scores']['em']['value'] is None
    assert method['scores']['em']['scored'] == 0
    assert method['usage']['total_tokens']['reported_total'] is None


def test_job_pins_materials_independently_of_original_deletion(monkeypatch):
    from src.knowledge.store import KnowledgeStore
    from src.runtime.system import retriever_from_snapshot
    knowledge = KnowledgeStore()
    material = knowledge.import_files('workspace', 'Notes', [('notes.txt', b'Cedar Institute is in Northbridge.')])
    systems, key = published_custom_system(material['source_id'], 1)
    original = systems.run_snapshot(key, 'v1')
    store = EvaluationStore(); bank = store.import_bank('Regression', '', questions(context=False))
    job = store.create_job(JobRequest(name='Pinned', bank_id=bank['bank_id'], bank_version=1,
        targets=[dict(kind='system', system_id=key, version='v1')], context_mode='system_sources'), systems)
    snapshot = next(iter(store.frozen(job['job_id'])[1]['definitions'].values()))['snapshot']
    for source in original['sources'].values():
        path = Path(source['resolved_path'])
        assert Path(os.environ['MAQA_KNOWLEDGE_DIR']) in path.parents
        path.unlink(missing_ok=True)
    for source in snapshot['sources'].values():
        assert store.root in Path(source['resolved_path']).parents
        assert retriever_from_snapshot({'source': source}).search('Cedar Northbridge')
    fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    assert store.job(job['job_id'])['report']['complete']


def test_tampered_environment_or_missing_runs_refuse_replay(monkeypatch):
    store = EvaluationStore(); job = new_job(store)
    fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    (store.job_directory(job['job_id']) / 'runs.sqlite3').unlink()
    store.queue(job['job_id'])
    with pytest.raises(EvaluationFailure, match='运行库缺失'):
        execute(store, job['job_id'], service_factory=RunService)
    store.state(job['job_id'], 'error')
    monkeypatch.setattr('src.evaluation.platform_store.identity', lambda: {'changed': True})
    with pytest.raises(EvaluationFailure, match='变化'):
        store.queue(job['job_id'])


def test_http_creation_gate_export_and_errors(monkeypatch):
    launches = []
    monkeypatch.setattr('src.evaluation.platform_worker.subprocess.Popen', lambda *a, **kw: launches.append(a))
    with TestClient(app) as client:
        response = client.post('/api/v1/evaluations/question-banks', data={'name': 'Web questions'},
                               files={'file': ('questions.json', json.dumps(questions()).encode(), 'application/json')})
        assert response.status_code == 201
        bank = response.json()
        assert client.get('/api/v1/evaluations/question-banks').json()['items'][0]['count'] == 2
        assert len(client.get('/api/v1/evaluations/options').json()['methods']) == 6
        response = client.post('/api/v1/evaluations/jobs', json=dict(name='Web job', bank_id=bank['bank_id'], bank_version=1,
            targets=[dict(kind='builtin', method='single_rag')]))
        assert response.status_code == 201, response.text
        job = response.json(); assert job['progress']['planned'] == 2 and job['status'] == 'draft'
        assert client.post(f"/api/v1/evaluations/jobs/{job['job_id']}/start", json={}).status_code == 422
        assert not launches
        assert client.get(f"/api/v1/evaluations/jobs/{job['job_id']}/export").status_code == 200
        assert client.get('/api/v1/evaluations/jobs/unknown').status_code == 404
        assert client.get('/api/v1/evaluations/jobs').json()['items'][0]['job_id'] == job['job_id']
        assert client.get(f"/api/v1/evaluations/jobs/{job['job_id']}/results").json()['total'] == 0
    assert "'=evil" in exported_csv([dict(question='=evil')])


def test_detached_worker_survives_api_client_close_with_local_stub(monkeypatch):
    """The real worker process talks only to a loopback OpenAI-shaped stub, never a provider."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import time
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(body)
            content = json.dumps(dict(answer='Northbridge', verified=False, failure_type='insufficient',
                                      supporting_evidence=[], verification_note='Local stub'))
            payload = json.dumps(dict(id='offline-response', object='chat.completion', created=1, model='offline-model',
                choices=[dict(index=0, message=dict(role='assistant', content=content), finish_reason='stop')],
                usage=dict(prompt_tokens=10, completion_tokens=10, total_tokens=20))).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(payload)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-not-a-provider-key')
    monkeypatch.setenv('DEEPSEEK_BASE_URL', f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('LANGSMITH_TRACING', 'false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'false')
    try:
        store = EvaluationStore()
        bank = store.import_bank('Detached test', '', [{**questions(1)[0], 'answer': 'GOLD_MUST_NEVER_BE_SENT'}])
        with TestClient(app) as client:
            response = client.post('/api/v1/evaluations/jobs', json=dict(name='Detached worker', bank_id=bank['bank_id'], bank_version=1,
                model=dict(name='offline-model'), targets=[dict(kind='builtin', method='single_rag')]))
            assert response.status_code == 201, response.text
            job_id = response.json()['job_id']
            assert client.post(f'/api/v1/evaluations/jobs/{job_id}/start', json={'allow_model_calls': True}).status_code == 200
        # The API lifespan is closed. The independent process still finishes and persists its report.
        # A fresh Windows Python process may spend over 35 seconds importing the
        # runtime before it can leave queued; keep the stub alive through startup.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            job = store.job(job_id)
            if job['report'] is not None or job['status'] == 'error':
                break
            time.sleep(.2)
        assert job['status'] == 'completed', (job, (store.job_directory(job_id) / 'worker.log').read_text(encoding='utf-8'))
        assert job['report']['complete'] and received
        assert job['report']['methods']['single_rag']['statuses']['completed'] == 1
        assert 'GOLD_MUST_NEVER_BE_SENT' not in json.dumps(received)
    finally:
        server.shutdown()
