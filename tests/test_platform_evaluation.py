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
from src.runtime.instances import templates
from src.runtime.protocols import protocol_templates_v1
from src.runtime.service import RunService
from src.runtime.system_store import SystemStore
from src.evaluation.platform_datasets import (ManifestRequest, SplitRequest, ExposureRequest,
    import_manifest, create_splits, add_exposures, get_manifest)


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    # Keep Windows paths below MAX_PATH, including the existing atomic context suffix.
    tmp_path = tmp_path_factory.mktemp('p')
    for env, folder in [('MAQA_EVALUATIONS_DIR', 'evaluations'), ('MAQA_SYSTEMS_DIR', 'systems'),
                        ('MAQA_KNOWLEDGE_DIR', 'knowledge'), ('MAQA_PROTOCOLS_DIR', 'protocols'), ('MAQA_EXPERIMENTS_DIR', 'experiments')]:
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
    assert result['retryable_count'] == 0
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
    assert store.job(job['job_id'])['retryable_count'] == 1
    calls = fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    assert not calls  # Default resume cannot replay failures.
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService, retry_failed=True)
    report = store.job(job['job_id'])['report']
    assert report['retry_count'] == 1
    assert report['methods']['single_rag']['scores']['em']['value'] == .5
    assert report['latest_attempt_supplement']['single_rag']['scores']['em']['value'] == 1
    assert store.job(job['job_id'])['retryable_count'] == 0


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
    assert store.job(job['job_id'])['retryable_count'] == 0  # Runtime already persisted completion.
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
    assert store.job(job['job_id'])['retryable_count'] == 1
    calls = fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    report = store.job(job['job_id'])['report']
    assert len(calls) == 1 and not report['complete']
    assert store.job(job['job_id'])['progress']['interrupted'] == 1


@pytest.mark.parametrize('damage', ['incomplete', 'corrupt'])
def test_reconcile_refuses_damaged_ledger_without_reconstructing(monkeypatch, damage):
    import sqlite3
    store = EvaluationStore(); job = new_job(store)
    fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    path = store.job_directory(job['job_id']) / 'tasks.sqlite3'
    if damage == 'incomplete':
        with sqlite3.connect(path) as db:
            db.execute('DROP TABLE sessions')
        db.close()
    else:
        path.write_bytes(b'not a sqlite database')
    before = path.read_bytes()
    store.state(job['job_id'], 'running')
    reconcile(store, job['job_id'])
    assert store.frozen(job['job_id'])[0]['status'] == 'error'
    assert path.read_bytes() == before


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


@pytest.mark.parametrize('status', ['failed', 'interrupted', 'cancelled'])
def test_retry_count_uses_latest_attempt_and_excludes_unstarted_tasks(status):
    store = EvaluationStore(); job = new_job(store)
    _, frozen = store.frozen(job['job_id'])
    directory = store.job_directory(job['job_id'])
    directory.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(directory)
    try:
        ledger.initialize(frozen['records'], SimpleNamespace(methods=['single_rag'], repeats=1, rotate_methods=True))
        task = ledger.tasks()[0]
        ledger.start(task, 'first'); ledger.finish('first', status, {})
        assert store.job(job['job_id'])['retryable_count'] == 1
        ledger.start(task, 'second'); ledger.finish('second', 'completed', {})
        current = store.job(job['job_id'])
        assert current['progress'][status] == 1
        assert current['progress']['attempted'] == 1
        assert current['retryable_count'] == 0
    finally:
        ledger.close()


@pytest.mark.parametrize('completed', [False, True])
def test_retry_start_without_candidates_preserves_job_and_never_launches(monkeypatch, completed):
    store = EvaluationStore(); job = new_job(store)
    if completed:
        fake_graph_phases(monkeypatch)
        store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    before = store.job(job['job_id'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Empty retries must not check credentials or launch a worker')
    monkeypatch.setattr('src.evaluation.platform_worker.require_env', forbidden)
    monkeypatch.setattr('src.evaluation.platform_worker.subprocess.Popen', forbidden)
    with TestClient(app) as client:
        response = client.post(f"/api/v1/evaluations/jobs/{job['job_id']}/start",
                               json=dict(allow_model_calls=True, retry_failed=True))
        assert response.status_code == 409
        assert '暂无可补跑项' in response.json()['detail']
    assert store.job(job['job_id']) == before
    assert not (store.job_directory(job['job_id']) / 'worker.log').exists()


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


def published_native_system(format, source_id='hotpotqa_fixture', source_version=None):
    systems = SystemStore()
    fields = dict(name=f'{format} system', workflow_id='multi_hop_qa_fixed',
                  source_id=source_id, source_version=source_version)
    if format != 'legacy':
        roles = [('planner', 'plan'), ('expert', 'expert_a'), ('expert', 'expert_b'), ('answer_validator', 'review')]
        if format == 'protocol_v1':
            roles = [('planner', 'plan'), ('retriever', 'retrieve'), ('answer_validator', 'review')]
        agents = []
        for role, key in roles:
            agent = deepcopy(next(t['defaults'] for t in templates() if t['agent_type'] == role))
            agent.update(agent_id=key, name=key)
            if role in ('expert', 'retriever'):
                agent.update(source_id=source_id, source_version=source_version)
            agents.append(agent)
        fields = dict(name=f'{format} system', agents=agents, workflow_id='multi_expert', execution_mode='sequential')
        if format == 'protocol_v1':
            protocols = ProtocolStore()
            spec = deepcopy(next(t['spec'] for t in protocol_templates_v1() if t['template_id'] == 'pipeline'))
            protocol = protocols.save(dict(name='V1 pipeline', spec=spec))
            protocols.publish(protocol['protocol_id'], 1)
            fields.pop('workflow_id')
            fields.update(protocol_ref=dict(protocol_id=protocol['protocol_id'], version='v1'),
                          protocol_bindings={slot: [a['agent_id'] for a in agents if a['agent_type'] == contract['role']]
                                             for slot, contract in spec['slots'].items()})
    item = systems.create(fields)
    systems.publish(item['system_id'], 1)
    fields['name'] += ' updated'
    systems.save(item['system_id'], 1, fields)
    systems.publish(item['system_id'], 2)
    return systems, item['system_id']


@pytest.mark.parametrize('format', ['legacy', 'instances', 'protocol_v1'])
@pytest.mark.parametrize('context_mode', ['question_context', 'system_sources'])
def test_all_native_versions_execute_with_frozen_materials_and_parameters(monkeypatch, format, context_mode):
    from src.knowledge.store import KnowledgeStore
    material = KnowledgeStore().import_files('workspace', 'Native notes', [('notes.txt', b'Cedar Institute is in Northbridge.')])
    systems, key = published_native_system(format, material['source_id'], 1)
    original = systems.run_snapshot(key, 'v1')
    before = systems.database.read_bytes()
    store = EvaluationStore()
    records = questions(1)
    records[0]['answer'] = 'GOLD_IS_PRIVATE'
    bank = store.import_bank('Native questions', '', records)
    override = dict(name='comparison-model', temperature=0.2, max_tokens=1200, output_retries=0) if context_mode == 'system_sources' else None
    job = store.create_job(JobRequest(name='Native versions', bank_id=bank['bank_id'], bank_version=1,
        targets=[dict(kind='system', system_id=key, version=v) for v in ('v1', 'v2')],
        context_mode=context_mode, model=override), systems)
    assert systems.database.read_bytes() == before
    frozen = store.frozen(job['job_id'])[1]
    assert len(frozen['definitions']) == 2
    assert {d['source']['version'] for d in frozen['definitions'].values()} == {'v1', 'v2'}
    for definition in frozen['definitions'].values():
        snapshot = definition['snapshot']
        assert snapshot['schema_version'] == original['schema_version']
        assert snapshot['workflow_spec'] == original['workflow_spec']
        for agent in snapshot['agents'].values():
            if agent.get('model_config'):
                assert agent['model_config']['system_prompt'] == original['agents'][agent['agent_id']]['model_config']['system_prompt']
                if override:
                    assert agent['model_config']['model_name'] == 'comparison-model'
                    assert agent['model_config']['max_tokens'] == 1200
    # Neither the saved system nor its material store is consulted during execution.
    systems.delete(key, 2)
    for source in (original.get('sources') or {'legacy': original['source']}).values():
        for field in ('resolved_path', 'index_path'):
            if field in source:
                Path(source[field]).unlink(missing_ok=True)
    seen = []
    class Chat:
        def __init__(self, config): self.config = config
        def invoke(self, prompt, **kwargs):
            seen.append((prompt, deepcopy(self.config)))
            return SimpleNamespace(content=json.dumps(dict(answer='Northbridge', verified=False, failure_type='insufficient',
                supporting_evidence=[], verification_note='Offline response')),
                usage_metadata=dict(input_tokens=10, output_tokens=5, total_tokens=15))
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda config: Chat(config))
    def plan(state, **kwargs):
        return dict(sub_questions=[dict(id='sq_1', question=state['question'], keywords=['Cedar'], depends_on=[])])
    for path in ('src.graph.planner_fn', 'src.runtime.nodes.planner_fn', 'src.runtime.protocol_runtime.planner_fn'):
        monkeypatch.setattr(path, plan)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    current = store.job(job['job_id'])
    assert current['status'] == 'completed' and current['progress']['completed'] == 2
    assert current['report']['complete'] and len(current['report']['methods']) == 2
    assert seen and 'GOLD_IS_PRIVATE' not in json.dumps(seen)
    for path in (store.job_directory(job['job_id']) / 'runs').glob('*.json'):
        detail = json.loads(path.read_text(encoding='utf-8'))
        assert detail['run']['status'] == 'completed'
        assert 'GOLD_IS_PRIVATE' not in json.dumps(detail)
        assert detail['run']['metrics']['total_tokens'] > 0


def test_options_include_every_published_version_and_explain_invalid_releases():
    from src.knowledge.store import KnowledgeStore
    material = KnowledgeStore().import_files('workspace', 'Broken notes', [('notes.txt', b'Cedar Institute is in Northbridge.')])
    systems, broken = published_native_system('instances', material['source_id'], 1)
    for format in ('legacy', 'instances', 'protocol_v1'):
        published_native_system(format)
    published_custom_system()
    draft = systems.create(dict(name='Unpublished', workflow_id='multi_hop_qa_fixed', source_id='hotpotqa_fixture'))
    snapshot = systems.run_snapshot(broken, 'v1')
    for source in snapshot['sources'].values():
        Path(source['resolved_path']).unlink(missing_ok=True)
    with TestClient(app) as client:
        choices = client.get('/api/v1/evaluations/options').json()['systems']
        assert len(choices) == 9
        assert all(c['system_id'] != draft['system_id'] for c in choices)
        unavailable = [c for c in choices if c['system_id'] == broken]
        assert len(unavailable) == 2
        assert all(not c['available'] and c['unavailable_reason'] for c in unavailable)
        assert all(c['available'] for c in choices if c['system_id'] != broken)
        bank = client.post('/api/v1/evaluations/question-banks', data={'name': 'Questions'},
            files={'file': ('q.json', json.dumps(questions(1)).encode(), 'application/json')}).json()
        response = client.post('/api/v1/evaluations/jobs', json=dict(name='Unavailable version',
            bank_id=bank['bank_id'], bank_version=1, targets=[dict(kind='system', system_id=broken, version='v1')]))
        assert response.status_code == 422 and '暂不可评测' in response.json()['detail']


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


def test_platform_splits_and_explicit_exposures_are_versioned_and_disjoint():
    store = EvaluationStore()
    bank = store.import_bank('Research data', '', questions(30))
    binding = dict(bank_id=bank['bank_id'], bank_version=1)
    add_exposures(store, ExposureRequest(**binding, sample_ids=['q29'], reason='Prior development'))
    fields = SplitRequest(**binding, name='Protocol', development=6, evaluation=20, smoke=2)
    first = create_splits(store, fields)['items']
    second = create_splits(store, fields)['items']
    ids = lambda doc: {r['sample_id'] for r in doc['samples']}
    dev, evaluation, smoke = first
    assert not ids(dev) & ids(evaluation) and ids(smoke) <= ids(dev)
    assert 'q29' not in ids(evaluation)
    assert [m['samples'] for m in first] == [m['samples'] for m in second]
    assert get_manifest(store, evaluation['manifest_id']) == evaluation
    store.import_bank('Research data v2', '', questions(30), bank['bank_id'])
    new = import_manifest(store, ManifestRequest(bank_id=bank['bank_id'], bank_version=2,
        name='Independent evaluation', purpose='evaluation'))
    assert not ids(new) & (ids(dev) | {'q29'})
    assert store.bank(bank['bank_id'], 1, public=True)['preview'] == []


def test_historical_platform_trials_seed_exposure_without_rewriting_jobs():
    store = EvaluationStore(); job = new_job(store)
    _, frozen = store.frozen(job['job_id'])
    from src.evaluation.experiment_config import digest
    frozen['request'].pop('purpose')
    frozen['sha256'] = digest({k: v for k, v in frozen.items() if k != 'sha256'})
    with store.connection() as db:
        db.execute('UPDATE jobs SET document=? WHERE id=?', (json.dumps(frozen), job['job_id']))
        db.execute('DELETE FROM exposures')
        db.execute("DELETE FROM platform_meta WHERE key='exposures-v1'")
    upgraded = EvaluationStore()
    assert upgraded.frozen(job['job_id'])[1] == frozen
    with upgraded.connection() as db:
        assert {r[0] for r in db.execute('SELECT sample_id FROM exposures')} == {'q0', 'q1'}
    EvaluationStore()
    assert upgraded.frozen(job['job_id'])[1] == frozen


def test_manifest_import_checks_source_hash_membership_purpose_and_exposure():
    import hashlib
    from src.evaluation.experiment_config import digest
    store = EvaluationStore()
    raw = json.dumps(questions(10)).encode()
    bank = store.import_bank('Original data', '', questions(10), raw_sha256=hashlib.sha256(raw).hexdigest())
    binding = dict(bank_id=bank['bank_id'], bank_version=1)
    source = dict(dataset_sha256=bank['raw_sha256'], purpose='evaluation', samples=[dict(sample_id='q3')])
    source['sha256'] = digest(source)
    fields = dict(**binding, name='Exact CLI selection', purpose='evaluation')
    manifest = import_manifest(store, ManifestRequest(**fields, source_manifest=source))
    assert [s['sample_id'] for s in manifest['samples']] == ['q3']
    for change in (dict(dataset_sha256='different'), dict(purpose='development'), dict(samples=[dict(sample_id='missing')]),
                   dict(samples=[dict(sample_id='q3'), dict(sample_id='q3')])):
        invalid = {**source, **change}; invalid['sha256'] = digest({k: v for k, v in invalid.items() if k != 'sha256'})
        with pytest.raises(EvaluationFailure):
            import_manifest(store, ManifestRequest(**fields, source_manifest=invalid))
    add_exposures(store, ExposureRequest(**binding, sample_ids=['q3'], reason='Human inspected'))
    with pytest.raises(EvaluationFailure, match='曝光'):
        import_manifest(store, ManifestRequest(**fields, source_manifest=source))


def test_formal_mixed_seven_object_preflight_matches_7000_tasks_without_execution():
    systems, key = published_custom_system()
    store = EvaluationStore(); bank = store.import_bank('Full experiment data', '', questions(1100))
    manifests = create_splits(store, SplitRequest(bank_id=bank['bank_id'], bank_version=1,
        name='Formal study', development=100, evaluation=1000, smoke=6))['items']
    evaluation = next(m for m in manifests if m['purpose'] == 'evaluation')
    fields = JobRequest(name='Six plus custom', bank_id=bank['bank_id'], bank_version=1, purpose='evaluation',
        manifest_id=evaluation['manifest_id'], targets=[dict(kind='builtin', method=m) for m in METHODS] +
        [dict(kind='system', system_id=key, version='v1')], bootstrap_samples=100)
    with pytest.raises(EvaluationFailure, match='启动前校验'):
        store.create_job(fields, systems)
    preview = store.preflight(fields, systems)
    assert preview['planned_tasks'] == 7000 and preview['model_calls'] == 0
    assert len(preview['targets']) == 7 and store.jobs() == []
    assert not (store.root / 'jobs').exists()
    assert next(t for t in preview['targets'] if t['method_id'] == 'lead')['limits']['max_llm_calls'] == 40
    changed = fields.model_copy(update=dict(bootstrap_samples=200, preflight_token=preview['token']))
    with pytest.raises(EvaluationFailure, match='重新执行'):
        store.create_job(changed, systems)
    job = store.create_job(fields.model_copy(update=dict(preflight_token=preview['token'])), systems)
    assert job['progress']['planned'] == 7000 and job['sample_count'] == 1000
    # Registering exposure after freezing is checked again before any credential lookup/worker launch.
    add_exposures(store, ExposureRequest(bank_id=bank['bank_id'], bank_version=1,
        sample_ids=[evaluation['samples'][0]['sample_id']], reason='Later human inspection'))
    with pytest.raises(EvaluationFailure, match='曝光'):
        start_job(store, job['job_id'], allow_model_calls=True)
    assert store.job(job['job_id'])['status'] == 'draft'


def test_mixed_formal_run_generates_paired_research_and_export_package(monkeypatch):
    import io
    import zipfile
    systems, key = published_custom_system()
    store = EvaluationStore(); bank = store.import_bank('Formal pair data', '', questions(2))
    manifest = import_manifest(store, ManifestRequest(bank_id=bank['bank_id'], bank_version=1,
        name='Fixed evaluation', purpose='evaluation'))
    fields = JobRequest(name='Mixed formal comparison', bank_id=bank['bank_id'], bank_version=1,
        purpose='evaluation', manifest_id=manifest['manifest_id'], repeats=2, bootstrap_samples=100,
        targets=[dict(kind='builtin', method=m) for m in METHODS] + [dict(kind='system', system_id=key, version='v1')])
    preview = store.preflight(fields, systems)
    job = store.create_job(fields.model_copy(update=dict(preflight_token=preview['token'])), systems)
    calls = fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    result = store.job(job['job_id'])
    research = result['report']['research']
    assert research['complete'] and len(research['pairs']) == 21
    assert len(research['reference_pairs']) == 6
    assert all(p['left'] == 'single_rag' for p in research['reference_pairs'])
    assert all(p['paired_samples'] == 2 for p in research['pairs'])
    assert all(p['metrics']['f1']['ci95_sample_bootstrap'] == [0, 0] for p in research['pairs'])
    assert research['methods']['single_rag']['repeat_score_std']['f1'] == 0
    assert research['methods']['single_rag']['by_type']['bridge']['planned'] == 4
    total_calls = len(calls)
    with TestClient(app) as client:
        for format in ('json', 'markdown', 'pairs_csv', 'zip'):
            response = client.get(f"/api/v1/evaluations/jobs/{job['job_id']}/report", params={'format': format})
            assert response.status_code == 200, response.text[:200] if format != 'zip' else ''
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert set(archive.namelist()) == {'report.md', 'paired-differences.csv', 'summary.json', 'frozen-plan.json', 'per-question.csv'}
    assert len(calls) == total_calls


def test_paired_statistics_do_not_invent_missing_labels_or_support_metrics():
    from src.evaluation.experiment_report import comparisons
    config = SimpleNamespace(methods=['single_rag', 'system_custom'], repeats=1, seed=42, bootstrap_samples=100)
    rows = [dict(method_id=m, sample_id=key, status='completed', scores=({'em': 1, 'f1': 1} if key == 'a' else None))
            for m in config.methods for key in ('a', 'b')]
    pair = comparisons(rows, config, ['a', 'b'])[0]
    assert pair['complete'] and all(v is None for v in pair['metrics'].values())
    pair = comparisons([r for r in rows if r['sample_id'] == 'a'], config, ['a'])[0]
    assert pair['metrics']['f1']['delta'] == 0 and pair['metrics']['sp_f1'] is None


def test_reference_comparisons_keep_object_minus_baseline_direction():
    from src.evaluation.platform_reports import relative_pairs
    pair = dict(left='object', right='baseline', metrics=dict(f1=dict(delta=0.2,
        ci95_sample_bootstrap=[0.1, 0.3], left_wins=1, right_wins=2, ties=3), sp_f1=None))
    normalized = relative_pairs([pair], 'baseline')[0]
    assert normalized['left'] == 'baseline' and normalized['right'] == 'object'
    assert normalized['metrics']['f1']['delta'] == -0.2
    assert normalized['metrics']['f1']['ci95_sample_bootstrap'] == [-0.3, -0.1]
    assert normalized['metrics']['f1']['left_wins'] == 2
    assert normalized['metrics']['sp_f1'] is None
    assert pair['metrics']['f1']['delta'] == 0.2


def test_review_separates_first_latest_filters_and_same_question_without_rewriting(monkeypatch):
    import hashlib
    store = EvaluationStore(); job = new_job(store, methods=['single_rag', 'pipeline_fixed'], records=questions(1))
    fake_graph_phases(monkeypatch, fail=True)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService)
    fake_graph_phases(monkeypatch)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=RunService, retry_failed=True)
    path = store.job_directory(job['job_id']) / 'tasks.sqlite3'
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    base = f"/api/v1/evaluations/jobs/{job['job_id']}"
    with TestClient(app) as client:
        first = client.get(base + '/results', params=dict(view='first', status='failed', label='runtime_failed', limit=1)).json()
        assert first['total'] == 2 and len(first['items']) == 1
        assert first['items'][0]['attempt'] == 1
        latest = client.get(base + '/results', params=dict(view='latest')).json()
        assert latest['total'] == 2 and all(r['attempt'] == 2 and r['status'] == 'completed' for r in latest['items'])
        assert client.get(base + '/results', params=dict(view='latest', label='runtime_failed')).json()['total'] == 0
        assert client.get(base + '/results', params=dict(view='all', method='single_rag')).json()['total'] == 2
        assert client.get(base + '/results', params=dict(search='%')).json()['total'] == 0
        compared = client.get(base + '/compare', params=dict(sample_id='q0', repeat=0, view='latest')).json()
        assert len(compared['items']) == 2 and all(r['status'] == 'completed' for r in compared['items'])
        assert client.get(base + '/compare', params=dict(sample_id='missing')).status_code == 404
        live = client.get(base + '/monitor').json()
        assert live['current'] is None
        assert sum(m['failed'] for m in live['methods']) == 2  # first attempts remain failures
        assert client.get(base).json()['report']['methods']['single_rag']['scores']['em']['value'] == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_live_monitor_tracks_parallel_phases_and_pending_answers():
    from datetime import datetime, timezone
    from src.runtime.models import RunRecord, RunMetrics, TraceEvent
    from src.evaluation.platform_review import monitor, compare_question
    store = EvaluationStore(); job = new_job(store, methods=['multi_expert'], records=questions(1))
    observed = []
    class Observe(RunService):
        def run(self, context):
            now = datetime.now(timezone.utc)
            self.repository.create_run(RunRecord(context=context, status='running', started_at=now))
            events = [('run_started', None, {}), ('node_started', 'experts_0', dict(call_id='a', stage_id='propose', agent_name='Expert A')),
                      ('node_started', 'experts_1', dict(call_id='b', stage_id='propose', agent_name='Expert B')),
                      ('node_completed', 'experts_0', dict(call_id='a', stage_id='propose'))]
            for sequence, (kind, node, payload) in enumerate(events):
                self.repository.append_event(TraceEvent(run_id=context.run_id, sequence=sequence,
                    event_type=kind, timestamp=now, node=node, cycle=1 if node else None, payload=payload))
            live = monitor(store, job['job_id'])
            observed.append(live)
            assert [p['agent_name'] for p in live['current']['phases']] == ['Expert B']
            compared = compare_question(store, job['job_id'], 'q0', 0, 'latest')
            assert compared['items'][0]['status'] == 'running'
            assert compared['items'][0]['run_id'] == context.run_id
            self.repository.append_event(TraceEvent(run_id=context.run_id, sequence=4, event_type='run_completed', timestamp=now))
            self.repository.update_run(RunRecord(context=context, status='completed', started_at=now, finished_at=now,
                final_state=dict(answer='Northbridge', verified=False), metrics=RunMetrics(duration_ms=100)))
            return self.repository.get_run_detail(context.run_id)
    store.queue(job['job_id']); execute(store, job['job_id'], service_factory=Observe)
    assert observed and monitor(store, job['job_id'])['current'] is None


def test_saved_plans_copy_and_archive_never_start_or_replace_original():
    from src.evaluation.platform_workspace import save_plan, get_plan, PlanRequest, copy_job_plan, archive_job
    store = EvaluationStore(); original = new_job(store)
    frozen = store.frozen(original['job_id'])[1]
    plan = save_plan(store, PlanRequest(name='Reusable conditions', source_job_id=original['job_id']))
    assert plan['request'].get('preflight_token') is None
    assert get_plan(store, plan['id']) == plan
    with pytest.raises(EvaluationFailure, match='指纹'):
        save_plan(store, PlanRequest(name='Tampered', request=plan['request'], sha256='wrong'))
    copied = copy_job_plan(store, original['job_id'])
    assert copied['request']['preflight_token'] is None and copied['needs_preflight']
    assert copied['request']['copied_from_job_id'] == original['job_id']
    fields = JobRequest.model_validate({**copied['request'], 'saved_plan_id': plan['id']})
    new = store.create_job(fields, SystemStore())
    assert new['job_id'] != original['job_id'] and new['status'] == 'draft'
    assert store.frozen(original['job_id'])[1] == frozen
    archive_job(store, original['job_id'], True)
    assert [j['job_id'] for j in store.jobs()] == [new['job_id']]
    assert len(store.jobs(include_archived=True)) == 2
    with pytest.raises(EvaluationFailure, match='已归档'):
        store.queue(original['job_id'])
    archive_job(store, original['job_id'], False)
    store.queue(original['job_id'])
    with pytest.raises(EvaluationFailure, match='运行中的任务'):
        archive_job(store, original['job_id'], True)
    with TestClient(app) as client:
        exported = client.get(f"/api/v1/evaluations/plans/{plan['id']}/export")
        assert exported.status_code == 200 and exported.json()['sha256'] == plan['sha256']
        assert client.get('/api/v1/evaluations/plans').json()['items'][0]['id'] == plan['id']


def cli_fixture(monkeypatch):
    from src.evaluation.experiment_config import Splits, atomic_json
    from src.evaluation.experiment_data import prepare_data
    from src.evaluation.experiment import execute as execute_cli
    root = Path(os.environ['MAQA_EXPERIMENTS_DIR']); root.mkdir()
    records = questions(6)
    for record in records: record['_id'] = record.pop('id')
    atomic_json(root / 'data.json', records)
    config = ExperimentConfig(dataset=str(root / 'data.json'), split_directory=str(root / 'splits'),
        output=str(root / 'historical-study'), purpose='evaluation', splits=Splits(development=2, evaluation=2, smoke=1),
        methods=['single_rag', 'pipeline_fixed'], bootstrap_samples=100)
    fake_graph_phases(monkeypatch)
    prepare_data(config)
    execute_cli(config, service_factory=RunService)
    return Path(config.output)


def test_cli_import_is_immutable_deduplicated_and_preserves_original_report(monkeypatch):
    import hashlib
    from src.evaluation.platform_archives import candidates, import_archive, get_archive
    path = cli_fixture(monkeypatch)
    before = {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest() for p in path.rglob('*') if p.is_file()}
    store = EvaluationStore()
    assert candidates()[0]['available']
    imported = import_archive(store, path.name)
    assert import_archive(store, path.name)['archive_id'] == imported['archive_id']
    assert imported['view']['readonly'] and imported['view']['progress']['completed'] == 4
    assert get_archive(store, imported['archive_id'])['original_report'] == imported['original_report']
    base = '/api/v1/evaluations/archives/' + imported['archive_id']
    with TestClient(app) as client:
        rows = client.get(base + '/results', params={'view': 'first', 'limit': 1}).json()
        assert rows['total'] == 4 and len(rows['items']) == 1
        row = rows['items'][0]
        detail = client.get(base + '/runs/' + row['run_id'])
        assert detail.status_code == 200 and detail.json()['run']['context']['experiment_id'] == path.name
        assert client.get(base + '/runs/foreign-run').status_code == 404
        assert len(client.get(base + '/compare', params={'sample_id': row['sample_id']}).json()['items']) == 2
        assert client.get(base + '/export', params={'format': 'json'}).json()['archive']['fingerprint'] == imported['fingerprint']
        assert client.post('/api/v1/evaluations/jobs/' + imported['archive_id'] + '/start', json={'allow_model_calls': True}).status_code == 404
    assert before == {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest() for p in path.rglob('*') if p.is_file()}
    # Snapshotted reports/results survive loss of the original pointer; trajectory lookup refuses drift.
    (path / 'reports/latest.json').write_text('{}')
    with TestClient(app) as client:
        assert client.get(base + '/results').json()['total'] == 4
        assert client.get(base).status_code == 200
        assert client.get(base + '/runs/' + row['run_id']).status_code in (409, 422)


def test_cli_import_rejects_active_sources_and_path_escape(monkeypatch):
    from src.evaluation.platform_archives import candidates, import_archive
    path = cli_fixture(monkeypatch); store = EvaluationStore()
    with experiment_lock(path):
        assert not candidates()[0]['available']
        with pytest.raises(EvaluationFailure, match='仍在运行'):
            import_archive(store, path.name)
    with pytest.raises(EvaluationFailure): import_archive(store, '../outside')
    (path / 'reports/latest.json').write_text(json.dumps(dict(path='../outside.json', sha256='x')))
    with pytest.raises(EvaluationFailure, match='路径超出'):
        import_archive(store, path.name)


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
    monkeypatch.setenv('MAQA_OFFLINE_HTTP_STUB', f'http://127.0.0.1:{server.server_port}/v1')
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
