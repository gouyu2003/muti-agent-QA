"""Stage 4.2 behavioural acceptance; all model calls are controlled doubles."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import re
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from src.agents.answer_validator import parse_verification
from src.knowledge.store import KnowledgeStore
from src.llm_usage import LLMUsageCollector
from src.runtime.context import build_v0_run_context
from src.runtime.instances import templates, compile_workflow, validate_instances
from src.runtime.nodes import evidence_ids
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.system_store import SystemStore, SystemFailure


def agent(role, key, source=None, **patch):
    value = deepcopy(next(t['defaults'] for t in templates() if t['agent_type'] == role))
    value.update(agent_id=key, name=key)
    if source:
        value.update(source_id=source, source_version=1 if source.startswith('user_') else None)
    value.update(patch)
    return value


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('MAQA_SYSTEMS_DIR', str(tmp_path / 'systems'))
    monkeypatch.setenv('MAQA_KNOWLEDGE_DIR', str(tmp_path / 'knowledge'))
    monkeypatch.setenv('MAQA_EXPERIMENT_CATALOG_PATH', str(tmp_path / 'missing.json'))
    monkeypatch.setenv('MAQA_RUN_DB_PATH', str(tmp_path / 'runs.sqlite3'))
    store, knowledge = SystemStore(), KnowledgeStore()
    sources = [knowledge.import_files('local', label, [('same.txt', body)])['source_id'] for label, body in
        [('A', b'Cedar Institute is in Northbridge.'), ('B', b'Cedar Institute was founded in 2008.')]]
    return store, knowledge, sources


def create(store, agents, workflow='multi_expert', mode='parallel'):
    return store.create({'name': 'Dynamic system', 'agents': agents, 'workflow_id': workflow,
                         'execution_mode': mode})


def execute(store, item, tmp_path):
    context = build_v0_run_context('Where and when was Cedar Institute founded?', entrypoint='api',
                                  system_id=item['system_id'], draft_revision=item['draft_revision'])
    return RunService(SQLiteRunRepository(tmp_path / 'runs.sqlite3')).run(context)


def fake_models(monkeypatch, barrier=None):
    seen, guard = [], Lock()
    class Chat:
        def __init__(self, config): self.config = config
        def invoke(self, prompt):
            key = self.config['agent_id']
            with guard: seen.append((key, prompt, deepcopy(self.config)))
            if barrier and self.config['role'] == 'expert': barrier.wait(timeout=10)
            refs = [json.loads(s) for s in re.findall(r'可直接复制的引用：(\{[^\n]+\})', prompt)]
            answer = {'answer': key, 'verified': bool(refs), 'failure_type': 'none' if refs else 'insufficient',
                      'supporting_evidence': refs, 'verification_note': 'Controlled model response'}
            return SimpleNamespace(content=json.dumps(answer), usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda config: Chat(config))
    return seen


@pytest.mark.parametrize('count', [1, 2, 3, 4, 5])
def test_real_graph_supports_one_to_five_instances_and_isolated_sources(env, tmp_path, monkeypatch, count):
    store, _, sources = env
    seen = fake_models(monkeypatch)
    if count == 1:
        agents = [agent('expert', 'medical_1', sources[0])]
    else:
        agents = [agent('expert', f'expert_{i}', sources[i % 2]) for i in range(min(count - 1, 3))]
        agents.append(agent('answer_validator', 'reviewer'))
        if count == 5:
            agents.insert(0, agent('planner', 'coordinator'))
            monkeypatch.setattr('src.runtime.nodes.planner_fn', lambda state, **kw: {
                'sub_questions': [{'id': 'sq_1', 'question': state['question'], 'keywords': [], 'depends_on': []}]})
    item = create(store, agents, 'single_expert' if count == 1 else 'multi_expert')
    detail = execute(store, item, tmp_path)
    assert detail.run.status == 'completed'
    assert detail.run.metrics.node_call_count == count
    assert len(detail.run.final_state['expert_results']) == min(count if count == 1 else count - 1, 3)
    assert detail.run.metrics.llm_call_count == len(seen)
    assert detail.run.metrics.total_tokens == 15 * len(seen)
    assert sum(v['call_count'] for v in detail.run.metrics.agent_usage.values()) == len(seen)
    for key, prompt, config in seen:
        if config['role'] == 'expert':
            assert '各专家独立观点' not in prompt
            assert config['model_config']['system_prompt'] in prompt
            assert len({r['source_id'] for r in re.findall(r'可直接复制的引用：(\{[^\n]+\})', prompt) for r in [json.loads(r)]}) <= 1
    if count >= 3:
        assert detail.run.context.source_id is None
        assert len({c['source_id'] for g in detail.run.final_state['evidence_chunks'] for c in g['chunks']}) == 2
    assert [e.sequence for e in detail.events] == list(range(len(detail.events)))
    assert detail.events[-1].event_type == 'run_completed'


@pytest.mark.parametrize('mode', ['sequential', 'parallel'])
def test_independent_experts_and_single_join(env, tmp_path, monkeypatch, mode):
    store, _, sources = env
    seen = fake_models(monkeypatch, Barrier(2) if mode == 'parallel' else None)
    item = create(store, [agent('expert', 'a', sources[0]), agent('expert', 'b', sources[1]),
                          agent('answer_validator', 'judge')], mode=mode)
    detail = execute(store, item, tmp_path)
    assert detail.run.status == 'completed'
    assert [key for key, _, _ in seen].count('judge') == 1
    assert seen[-1][0] == 'judge'
    assert '各专家独立观点' in seen[-1][1]
    assert set(detail.run.final_state['expert_results']) == {'a', 'b'}


def test_parameters_reach_independent_models_and_retrievers(env, tmp_path, monkeypatch):
    store, _, sources = env
    agents = [agent('expert', 'a', sources[0]), agent('expert', 'b', sources[1]), agent('answer_validator', 'judge')]
    for index, a in enumerate(agents):
        a['model_config'].update(model_name=f'model-{index}', system_prompt=f'unique prompt {index}', temperature=index * .2, max_tokens=1024 + index)
        a['top_k'] = index + 1
    seen = fake_models(monkeypatch)
    from src.runtime import nodes
    original = nodes.retriever_fn
    top_ks = []
    def capture(state, **kw):
        top_ks.append((kw['agent_config']['agent_id'], kw['agent_config']['retrieval_config']['top_k']))
        return original(state, **kw)
    monkeypatch.setattr(nodes, 'retriever_fn', capture)
    result = execute(store, create(store, agents), tmp_path)
    assert result.run.context.model_name == 'multiple'
    assert sorted(top_ks) == [('a', 1), ('b', 2)]
    assert {key: config['model_config'] for key, _, config in seen} == {a['agent_id']: a['model_config'] for a in agents}


def test_custom_pipeline_ids_and_feedback_route(env, tmp_path, monkeypatch):
    store, _, sources = env
    agents = [agent('planner', 'plan_custom'), agent('retriever', 'search_custom', sources[0]), agent('answer_validator', 'check_custom')]
    fake_models(monkeypatch)
    monkeypatch.setattr('src.runtime.nodes.planner_fn', lambda state, **kw: {
        'sub_questions': [{'id': 'sq_1', 'question': 'Cedar Institute', 'keywords': [], 'depends_on': []}]})
    for template in ('multi_hop_qa_fixed', 'multi_hop_qa_v0'):
        detail = execute(store, create(store, agents, template, 'sequential'), tmp_path)
        assert detail.run.status == 'completed'
        assert {e.node for e in detail.events if e.event_type == 'node_completed'} == {'plan_custom', 'search_custom', 'check_custom'}
        assert detail.run.final_state['verified']
        route = next(e for e in detail.events if e.event_type == 'route_decided')
        assert route.node == 'check_custom' and route.payload['target'] == '__end__'


def test_draft_constraints_and_immutable_type(env):
    store, _, sources = env
    a = agent('expert', 'one', sources[0])
    for agents in ([], [a] * 2, [agent('expert', f'a{i}', sources[0]) for i in range(6)],
                   [{**a, 'agent_type': 'python'}], [{**a, 'top_k': True}]):
        with pytest.raises(ValueError): create(store, agents)
    item = create(store, [a], 'multi_expert')
    assert item['draft']['snapshot']['configuration_errors']
    with pytest.raises(SystemFailure): store.publish(item['system_id'], 1)
    changed = agent('planner', 'one')
    with pytest.raises(SystemFailure, match='类型不能修改'):
        store.save(item['system_id'], 1, {'name': 'Changed', 'agents': [changed], 'workflow_id': 'single_expert'})


def test_versions_freeze_templates_parameters_and_materials(env, tmp_path, monkeypatch):
    store, knowledge, sources = env
    a = agent('expert', 'a', sources[0])
    item = create(store, [a], 'single_expert')
    first = store.publish(item['system_id'], 1)
    knowledge.import_files('local', None, [('same.txt', b'Cedar Institute moved to Southbridge.')], sources[0])
    a['name'] = 'Renamed'; a['source_version'] = 2
    a['model_config']['system_prompt'] = 'Changed prompt'
    store.save(item['system_id'], 1, {'name': 'Changed', 'workflow_id': 'single_expert', 'agents': [a]})
    store.publish(item['system_id'], 2)
    assert SystemStore().version(item['system_id'], 'v1') == first
    old = store.run_snapshot(item['system_id'], 'v1')
    assert old['sources']['a']['version'] == 1
    assert old['agents']['a']['agent_name'] == 'a'
    source = old['sources']['a']
    __import__('pathlib').Path(source['resolved_path']).write_text('{}')
    with pytest.raises(SystemFailure, match='校验失败'): store.run_snapshot(item['system_id'], 'v1')


def test_evidence_identity_prevents_identical_chunk_cross_source_citation():
    def groups(source, version):
        return evidence_ids([{'sq_id': 'sq_1', 'sufficiency': 'unknown', 'chunks': [
            {'text': 'Cedar Institute is in Northbridge.', 'source_title': 'same.txt', 'chunk_id': 'samehash',
             'source_id': source, 'source_version': version, 'relevance_score': 1}]}])
    a, b = groups('a', 1), groups('b', 1)
    assert a[0]['chunks'][0]['evidence_id'] != b[0]['chunks'][0]['evidence_id']
    state = {'sub_questions': [{'id': 'sq_1'}], 'evidence_chunks': [*a, *b]}
    result = parse_verification(json.dumps({'answer': 'Northbridge', 'verified': True,
        'supporting_evidence': [{'sq_id': 'sq_1', 'evidence_id': b[0]['chunks'][0]['evidence_id']}]}), state)
    assert result['verified'] and result['supporting_evidence'][0]['source_id'] == 'b'
    result = parse_verification(json.dumps({'answer': 'Northbridge', 'verified': True,
        'supporting_evidence': [{'sq_id': 'sq_1', 'evidence_id': 'forged'}]}), state)
    assert not result['verified']


def test_api_structured_instances_and_pinned_version_run(env, monkeypatch, tmp_path):
    store, _, sources = env
    fake_models(monkeypatch)
    with TestClient(app) as client:
        assert {i['agent_type'] for i in client.get('/api/v1/agent-templates').json()['items']} == {'planner', 'retriever', 'answer_validator', 'expert', 'lead'}
        fields = {'name': 'API', 'workflow_id': 'single_expert', 'agents': [agent('expert', 'a', sources[0])]}
        response = client.post('/api/v1/systems', json=fields)
        assert response.status_code == 201, response.text
        item = response.json()
        assert item['draft']['snapshot']['agents']['a']['model_config']['model_name']
        key = item['system_id']
        assert client.post(f'/api/v1/systems/{key}/versions', json={'expected_revision': 1}).status_code == 200
        result = client.post('/api/v1/qa/stream', json={'question': 'Where is Cedar Institute?', 'system_id': key, 'system_version': 'v1'})
        assert 'event: run_completed' in result.text
        history = client.get('/api/v1/runs', params={'system_id': key}).json()
        assert history['total'] == 1
        run_id = history['items'][0]['context']['run_id']
        detail = client.get(f'/api/v1/runs/{run_id}').json()
        assert detail['run']['final_state']['expert_results']['a']['answer'] == 'a'
        assert client.post('/api/v1/systems', json={**fields, 'snapshot': {}}).status_code == 422
        assert client.put(f'/api/v1/systems/{key}/draft', json={**fields, 'expected_revision': 99}).status_code == 409


def test_parallel_usage_missing_data_and_calls_are_truthful():
    collector = LLMUsageCollector()
    class Chat:
        def invoke(self, prompt): return SimpleNamespace(content='ok', usage_metadata={'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5})
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda i: collector.for_agent(str(i % 3)).invoke(Chat(), 'q'), range(100)))
    assert collector.snapshot().call_count == 100 and collector.snapshot().total_tokens == 500
    collector.for_agent('unknown').invoke(SimpleNamespace(invoke=lambda _: SimpleNamespace(content='ok')), 'q')
    assert collector.snapshot().call_count == 101 and collector.snapshot().total_tokens is None


def test_reviewer_uses_combined_evidence_even_when_first_expert_misses_a_subquestion(monkeypatch):
    from src.runtime.nodes import review_node
    seen = []
    monkeypatch.setattr('src.runtime.nodes.answer_validator_fn', lambda state, **_: seen.append(state) or {})
    empty = {'sq_id': 'sq_1', 'chunks': [], 'sufficiency': 'insufficient', 'insufficiency_note': 'No evidence in A'}
    hit = {'sq_id': 'sq_1', 'chunks': [{'evidence_id': 'b', 'text': 'Evidence from B'}], 'sufficiency': 'unknown', 'insufficiency_note': ''}
    state = {'question': 'Q', 'expert_results': {'a': {'evidence_chunks': [empty]}, 'b': {'evidence_chunks': [hit]}}}
    review_node(state, config={}, usage=LLMUsageCollector(), expert_ids=['a', 'b'])
    assert seen[0]['evidence_chunks'][0]['sufficiency'] == 'unknown'
    assert not seen[0]['evidence_chunks'][0]['insufficiency_note']


def test_cancel_parallel_run_settles_workers_and_preserves_terminal(env, monkeypatch, tmp_path):
    store, _, sources = env
    entered, release, finished = Event(), Event(), Event()
    called = []
    def verify(state, **kwargs):
        key = kwargs['agent_config']['agent_id']
        called.append(key)
        def model_call(_):
            if key == 'b':
                entered.set(); assert release.wait(15)
            else:
                assert entered.wait(10)
            return SimpleNamespace(content='partial', usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
        kwargs['usage_collector'].invoke(SimpleNamespace(invoke=model_call), 'q')
        return {'answer': 'partial', 'verified': False, 'supporting_evidence': []}
    monkeypatch.setattr('src.runtime.nodes.answer_validator_fn', verify)
    item = create(store, [agent('expert', 'a', sources[0]), agent('expert', 'b', sources[1]), agent('answer_validator', 'judge')])
    context = build_v0_run_context('Cedar Institute?', entrypoint='api', system_id=item['system_id'], draft_revision=1)
    repository = SQLiteRunRepository(tmp_path / 'runs.sqlite3')
    events = RunService(repository).stream(context)
    next(events)
    while True:
        event = next(events)
        if event.event_type == 'node_completed' and event.node == 'a': break
    assert entered.wait(10)
    def stop():
        events.close(); finished.set()
    thread = Thread(target=stop); thread.start()
    release.set(); thread.join(15)
    assert finished.is_set()
    detail = repository.get_run_detail(context.run_id)
    assert detail.run.status == 'cancelled' and detail.events[-1].event_type == 'run_cancelled'
    assert detail.run.metrics.llm_call_count == 2 and detail.run.metrics.total_tokens == 30
    assert 'judge' not in called
    before = detail.model_dump()
    assert repository.get_run_detail(context.run_id).model_dump() == before


@pytest.mark.parametrize('failure', ['insufficient', 'conflict'])
def test_unanswerable_or_conflicting_evidence_is_a_completed_unverified_result(env, tmp_path, monkeypatch, failure):
    store, _, sources = env
    class Chat:
        def invoke(self, prompt):
            return SimpleNamespace(content=json.dumps({'answer': '', 'verified': False, 'failure_type': failure,
                'conflict_flag': failure == 'conflict', 'verification_note': 'Evidence does not justify an answer.'}))
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda _: Chat())
    item = create(store, [agent('expert', 'a', sources[0]), agent('answer_validator', 'judge')])
    result = execute(store, item, tmp_path)
    assert result.run.status == 'completed'
    assert not result.run.final_state['verified'] and result.run.final_state['failure_type'] == failure


def test_failed_parallel_branch_preserves_other_expert_and_skips_review(env, tmp_path, monkeypatch):
    store, _, sources = env
    ready = Event()
    def verify(state, **kwargs):
        if kwargs['agent_config']['agent_id'] == 'b':
            assert ready.wait(10)
            raise RuntimeError('controlled failure')
        ready.set()
        return {'answer': 'partial', 'verified': False, 'supporting_evidence': []}
    monkeypatch.setattr('src.runtime.nodes.answer_validator_fn', verify)
    item = create(store, [agent('expert', 'a', sources[0]), agent('expert', 'b', sources[1]), agent('answer_validator', 'judge')])
    result = execute(store, item, tmp_path)
    assert result.run.status == 'failed'
    assert 'a' in result.run.final_state['expert_results']
    assert not any(e.node == 'judge' for e in result.events)
    assert result.events[-1].event_type == 'run_failed'


@pytest.mark.parametrize('failure', ['insufficient', 'conflict'])
def test_custom_pipeline_feedback_retries_correct_role_and_keeps_counters(env, tmp_path, monkeypatch, failure):
    store, _, sources = env
    attempts = []
    class Planner:
        def invoke(self, prompt):
            return SimpleNamespace(content=json.dumps({'sub_questions': [{'id': 'sq_1', 'question': 'Cedar Institute', 'keywords': [], 'depends_on': []}]}))
    class Validator:
        def invoke(self, prompt):
            attempts.append(prompt)
            refs = [json.loads(s) for s in re.findall(r'可直接复制的引用：(\{[^\n]+\})', prompt)]
            return SimpleNamespace(content=json.dumps({'answer': 'Northbridge' if len(attempts) > 1 else '',
                'verified': len(attempts) > 1, 'failure_type': 'none' if len(attempts) > 1 else failure,
                'conflict_flag': len(attempts) == 1 and failure == 'conflict', 'supporting_evidence': refs}))
    monkeypatch.setattr('src.agents.planner.default_llm', lambda _: Planner())
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda _: Validator())
    item = create(store, [agent('planner', 'p'), agent('retriever', 'r', sources[0]), agent('answer_validator', 'v')],
                  'multi_hop_qa_v0', 'sequential')
    detail = execute(store, item, tmp_path)
    assert detail.run.status == 'completed' and detail.run.final_state['verified']
    assert len(attempts) == 2
    route = next(e for e in detail.events if e.event_type == 'route_decided')
    assert route.payload['target'] == ('r' if failure == 'insufficient' else 'p')
    assert detail.run.final_state['retrieval_retry_count' if failure == 'insufficient' else 'plan_retry_count'] == 1
