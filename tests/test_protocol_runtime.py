from copy import deepcopy
import json
import re
from types import SimpleNamespace
import pytest

from src.runtime.protocols import protocol_templates
from src.runtime.protocol_store import ProtocolStore
from src.runtime.instances import templates
from src.runtime.system_store import SystemStore, SystemFailure
from src.runtime.context import build_v0_run_context
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.service import RunService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for name, child in [('MAQA_PROTOCOLS_DIR', 'protocols'), ('MAQA_SYSTEMS_DIR', 'systems'), ('MAQA_KNOWLEDGE_DIR', 'knowledge')]:
        monkeypatch.setenv(name, str(tmp_path / child))
    monkeypatch.setenv('MAQA_RUN_DB_PATH', str(tmp_path / 'runs.sqlite3'))
    monkeypatch.setenv('MAQA_EXPERIMENT_CATALOG_PATH', str(tmp_path / 'missing.json'))
    return ProtocolStore(), SystemStore()


def instance(role, key):
    value = deepcopy(next(t['defaults'] for t in templates() if t['agent_type'] == role))
    value.update(agent_id=key, name=key)
    if role in ('retriever', 'expert'): value.update(source_id='hotpotqa_fixture', source_version=None)
    return value


def system(workspace, family='pipeline', patch=None, draft=False):
    protocols, systems = workspace
    spec = deepcopy(next(t['spec'] for t in protocol_templates() if t['template_id'] == family))
    if patch: patch(spec)
    protocol = protocols.save(dict(name='User protocol', spec=spec))
    release = protocols.publish(protocol['protocol_id'], 1)
    roles = {'pipeline': [('planner', 'p'), ('retriever', 'r'), ('answer_validator', 'v')],
             'lead': [('lead', 'boss'), ('expert', 'a'), ('expert', 'b'), ('answer_validator', 'v')],
             'debate': [('expert', 'a'), ('expert', 'b'), ('answer_validator', 'v')]}[family]
    agents = [instance(r, i) for r, i in roles]
    bindings = {slot: [a['agent_id'] for a in agents if a['agent_type'] == contract['role']] for slot, contract in spec['slots'].items()}
    item = systems.create(dict(name='Protocol system', agents=agents,
        protocol_ref=dict(protocol_id=release['protocol_id'], **({'revision': 1} if draft else {'version': 'v1'})), protocol_bindings=bindings))
    assert not item['draft']['snapshot']['configuration_errors']
    return item


def execute(workspace, item, tmp_path):
    context = build_v0_run_context('Who held the Chief of Protocol position?', entrypoint='api', system_id=item['system_id'], draft_revision=item['draft_revision'])
    return RunService(SQLiteRunRepository(tmp_path / 'runs.sqlite3')).run(context)


def fake_pipeline(monkeypatch, verified=True):
    monkeypatch.setattr('src.runtime.protocol_runtime.planner_fn', lambda state, **kw: {'sub_questions': [dict(id='sq_1', question=state['question'], keywords=['Protocol'], depends_on=[])]})
    class Chat:
        def invoke(self, prompt, **kwargs):
            refs = [json.loads(r) for r in re.findall(r'可直接复制的引用：(\{[^\n]+\})', prompt)]
            return SimpleNamespace(content=json.dumps(dict(answer='Test', verified=verified, failure_type='none' if verified else 'insufficient',
                supporting_evidence=refs, verification_note='Evidence grounded')), usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda config: Chat())


def test_pipeline_compilation_invocations_and_immutable_system(workspace, tmp_path, monkeypatch):
    fake_pipeline(monkeypatch)
    item = system(workspace)
    published = workspace[1].publish(item['system_id'], 1)
    frozen = deepcopy(published)
    detail = execute(workspace, item, tmp_path)
    assert detail.run.status == 'completed'
    calls = detail.run.final_state['protocol_calls']
    assert len(calls) == 3
    assert {c['agent_id'] for c in calls.values()} == {'p', 'r', 'v'}
    assert {c['stage_id'] for c in calls.values()} == {'plan', 'retrieve', 'review'}
    assert all(key == c['call_id'] for key, c in calls.items())
    assert detail.run.metrics.llm_call_count == 1
    assert detail.run.metrics.agent_usage['v']['call_count'] == 1
    protocols = workspace[0]
    doc = protocols.get(item['draft']['protocol_ref']['protocol_id'])
    fields = deepcopy(doc['draft']); fields['name'] = 'Changed protocol'
    protocols.save(fields, doc['protocol_id'], 1); protocols.publish(doc['protocol_id'], 2)
    assert workspace[1].version(item['system_id'], 'v1') == frozen
    assert SQLiteRunRepository(tmp_path / 'runs.sqlite3').get_run_detail(detail.run.context.run_id) == detail


def test_pipeline_finite_retry_keeps_every_output(workspace, tmp_path, monkeypatch):
    fake_pipeline(monkeypatch, False)
    def patch(spec):
        spec['stages'][1]['max_visits'] = 2
        spec['stages'][2].update(max_visits=2, routes=[dict(condition='verified', target='END'), dict(condition='unverified', target='retrieve'), dict(condition='fallback', target='END')])
    detail = execute(workspace, system(workspace, patch=patch), tmp_path)
    assert detail.run.status == 'completed'
    calls = list(detail.run.final_state['protocol_calls'].values())
    assert len(calls) == 5
    assert sorted(c['round'] for c in calls if c['agent_id'] == 'r') == [1, 2]
    assert detail.run.final_state['termination_reason'] == 'max_visits'


def test_saved_protocol_draft_trial_cannot_publish_system(workspace):
    item = system(workspace, draft=True)
    workspace[1].run_snapshot(item['system_id'], revision=1)
    with pytest.raises(SystemFailure): workspace[1].publish(item['system_id'], 1)


@pytest.mark.parametrize('limit', ['max_node_calls', 'max_llm_calls', 'max_reserved_tokens'])
def test_resource_budgets_preserve_completed_calls(workspace, tmp_path, monkeypatch, limit):
    fake_pipeline(monkeypatch, False)
    def patch(spec):
        spec['limits'][limit] = 1
        spec['stages'][1]['max_visits'] = 2
        spec['stages'][2].update(max_visits=2, routes=[dict(condition='unverified', target='retrieve'), dict(condition='fallback', target='END')])
    detail = execute(workspace, system(workspace, patch=patch), tmp_path)
    assert detail.run.status == 'failed'
    assert detail.run.error.error_type == 'protocol_execution_error'
    assert detail.run.final_state['protocol_calls']
    assert len([e for e in detail.events if e.event_type in ('run_completed', 'run_failed', 'run_cancelled')]) == 1


def fake_lead_models(monkeypatch, decisions, barrier=None):
    from threading import Lock
    seen, lock = [], Lock()
    class Chat:
        def __init__(self, config): self.config = config
        def invoke(self, prompt, **kwargs):
            with lock:
                seen.append((self.config['agent_id'], prompt))
                if self.config['role'] == 'lead':
                    result = decisions.pop(0) if len(decisions) > 1 else decisions[0]
                else: result = None
            if result is None:
                if barrier and self.config['role'] == 'expert': barrier.wait(timeout=10)
                refs = [json.loads(r) for r in re.findall(r'可直接复制的引用：(\{[^\n]+\})', prompt)]
                result = dict(answer=self.config['agent_id'], verified=bool(refs), failure_type='none' if refs else 'insufficient',
                              supporting_evidence=refs, verification_note='Evidence grounded')
            return SimpleNamespace(content=json.dumps(result), usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda config: Chat(config))
    return seen


def dispatch(*ids, instruction='Find the person who served as Chief of Protocol'):
    return dict(action='dispatch', tasks=[dict(task_id=f'task_{i}', agent_id=key, instruction=instruction) for i, key in enumerate(ids)], reason='Selected relevant expert')


def reviewed(): return dict(action='review', tasks=[], summary='Evidence-grounded synthesis', reason='Completed assigned work')


def test_lead_selects_only_dispatched_expert_and_passes_concrete_task(workspace, tmp_path, monkeypatch):
    seen = fake_lead_models(monkeypatch, [dispatch('a', instruction='Find the person in the Protocol position'), reviewed()])
    detail = execute(workspace, system(workspace, 'lead'), tmp_path)
    assert detail.run.status == 'completed'
    assert [key for key, _ in seen] == ['boss', 'a', 'boss', 'v']
    assert '本次分派的具体任务：Find the person in the Protocol position' in seen[1][1]
    calls = list(detail.run.final_state['protocol_calls'].values())
    assert len(calls) == 4
    assert len([c for c in calls if c['kind'] == 'dispatch']) == 1
    routes = [e.payload for e in detail.events if e.event_type == 'route_decided' and e.payload['stage_id'] == 'coordinate']
    assert [r['target'] for r in routes] == ['work', 'review']
    assert detail.run.metrics.agent_usage['boss']['call_count'] == 2


def test_lead_second_round_results_and_inputs_are_preserved(workspace, tmp_path, monkeypatch):
    seen = fake_lead_models(monkeypatch, [dispatch('a', instruction='Find the first name'), dispatch('b', instruction='Find the role title'), reviewed()])
    detail = execute(workspace, system(workspace, 'lead'), tmp_path)
    assert detail.run.status == 'completed'
    work = sorted([c for c in detail.run.final_state['protocol_calls'].values() if c['kind'] == 'dispatch'], key=lambda c: c['round'])
    assert [(c['agent_id'], c['round']) for c in work] == [('a', 1), ('b', 2)]
    assert work[0]['input']['task']['instruction'] == 'Find the first name'
    assert work[1]['input']['task']['instruction'] == 'Find the role title'
    assert all(c['call_id'] in seen[-1][1] for c in work)
    assert len({c['call_id'] for c in detail.run.final_state['protocol_calls'].values()}) == 6


def test_lead_parallel_join_waits_for_selected_tasks(workspace, tmp_path, monkeypatch):
    from threading import Barrier
    fake_lead_models(monkeypatch, [dispatch('a', 'b'), reviewed()], Barrier(2))
    detail = execute(workspace, system(workspace, 'lead'), tmp_path)
    assert detail.run.status == 'completed'
    events = detail.events
    completed = [i for i, e in enumerate(events) if e.event_type == 'node_completed' and e.payload.get('kind') == 'dispatch']
    lead_again = next(i for i, e in enumerate(events) if e.event_type == 'node_started' and e.node == 'boss' and e.cycle == 2)
    assert len(completed) == 2 and max(completed) < lead_again


def test_same_agent_can_run_two_parallel_tasks_without_overwriting(workspace, tmp_path, monkeypatch):
    from threading import Barrier
    fake_lead_models(monkeypatch, [dispatch('a', 'a'), reviewed()], Barrier(2))
    detail = execute(workspace, system(workspace, 'lead'), tmp_path)
    assert detail.run.status == 'completed'
    calls = [c for c in detail.run.final_state['protocol_calls'].values() if c['kind'] == 'dispatch']
    assert len(calls) == 2 and {c['agent_id'] for c in calls} == {'a'}
    assert len({c['task_id'] for c in calls}) == 2 and len({c['call_id'] for c in calls}) == 2
    assert detail.run.metrics.agent_usage['a']['call_count'] == 2


def test_lead_unregistered_targets_fail_without_executing_experts(workspace, tmp_path, monkeypatch):
    seen = fake_lead_models(monkeypatch, [dispatch('unknown')])
    detail = execute(workspace, system(workspace, 'lead'), tmp_path)
    assert detail.run.status == 'failed'
    assert {key for key, _ in seen} == {'boss'}
    assert detail.run.error.error_type == 'protocol_execution_error'


def test_lead_continuing_dispatch_is_bounded_and_goes_to_review(workspace, tmp_path, monkeypatch):
    seen = fake_lead_models(monkeypatch, [dispatch('a')])
    detail = execute(workspace, system(workspace, 'lead'), tmp_path)
    assert detail.run.status == 'completed'
    assert [key for key, _ in seen].count('a') == 2
    assert seen[-1][0] == 'v'
    assert any(e.payload.get('reason') == 'max_visits' for e in detail.events)


def fake_debate_models(monkeypatch, rounds=1, invalid_reference=False):
    from threading import Lock
    seen, lock = [], Lock()
    reviews = 0
    class Chat:
        def __init__(self, config): self.config = config
        def invoke(self, prompt, **kwargs):
            nonlocal reviews
            with lock: seen.append((self.config['agent_id'], prompt))
            if '\n{"stage":' in prompt:
                request = json.loads(prompt[prompt.index('\n{"stage":') + 1:])
                if request['stage'] == 'critique':
                    result = dict(critiques=[dict(target_call_id=key, stance='challenge', issue='Check the supporting evidence',
                        evidence_ids=['unknown'] if invalid_reference else []) for key in request['target_call_ids']], summary='Public evidence challenge')
                else:
                    refs = [dict(sq_id=g['sq_id'], evidence_id=c['evidence_id']) for g in request['evidence'] for c in g['chunks']]
                    result = dict(answer='ConsensusAnswer' if refs else '', verified=bool(refs), failure_type='none' if refs else 'insufficient',
                        supporting_evidence=refs, verification_note='Reviewed source evidence',
                        responses=[dict(critique_call_id=key, accepted=True, response='Checked and revised against evidence') for key in request['incoming_critique_call_ids']],
                        change_note='Revised the original proposal after checking the challenge')
            else:
                refs = [json.loads(r) for r in re.findall(r'可直接复制的引用：(\{[^\n]+\})', prompt)]
                verified = bool(refs)
                if self.config['role'] == 'answer_validator':
                    reviews += 1; verified = verified and reviews >= rounds
                result = dict(answer='Final' if self.config['role'] == 'answer_validator' else f'Initial_{self.config["agent_id"]}',
                    verified=verified, failure_type='none' if verified else 'conflict', supporting_evidence=refs, verification_note='Controlled evidence response')
            return SimpleNamespace(content=json.dumps(result), usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda config: Chat(config))
    return seen


def test_debate_independent_proposals_challenges_and_revisions(workspace, tmp_path, monkeypatch):
    seen = fake_debate_models(monkeypatch)
    detail = execute(workspace, system(workspace, 'debate'), tmp_path)
    assert detail.run.status == 'completed'
    calls = list(detail.run.final_state['protocol_calls'].values())
    assert len(calls) == 7
    proposals = [c for c in calls if c['kind'] == 'propose']
    critiques = [c for c in calls if c['kind'] == 'critique']
    revisions = [c for c in calls if c['kind'] == 'revise']
    assert all(not c['input']['reads'] for c in proposals)
    for c in proposals:
        prompt = next(p for key, p in seen if key == c['agent_id'])
        assert 'Initial_' not in prompt
    for c in critiques:
        targets = {ch['target_call_id'] for ch in c['output']['critiques']}
        assert targets == {p['call_id'] for p in proposals if p['agent_id'] != c['agent_id']}
        assert not any(r['kind'] == 'critique' for r in c['input']['reads'])
    assert all(c['output']['responses'] and c['output']['change_note'] for c in revisions)
    assert {c['output']['answer'] for c in proposals} == {'Initial_a', 'Initial_b'}
    assert {c['output']['answer'] for c in revisions} == {'ConsensusAnswer'}
    assert detail.run.final_state['consensus_status'] == 'agreement'
    assert detail.run.metrics.llm_call_count == 7


def test_debate_multiple_rounds_never_overwrite_outputs(workspace, tmp_path, monkeypatch):
    fake_debate_models(monkeypatch, rounds=2)
    detail = execute(workspace, system(workspace, 'debate'), tmp_path)
    assert detail.run.status == 'completed'
    calls = list(detail.run.final_state['protocol_calls'].values())
    assert len(calls) == 12
    assert len({c['call_id'] for c in calls}) == 12
    assert sorted(c['round'] for c in calls if c['kind'] == 'revise' and c['agent_id'] == 'a') == [1, 2]
    assert len([c for c in calls if c['kind'] == 'propose']) == 2
    for c in calls:
        assert c['call_id'] != c['agent_id'] and c['call_id'] != c['stage_id']
        assert not any(r['stage_id'] == c['stage_id'] and r['round'] == c['round'] for r in c['input']['reads'])


def test_debate_invalid_evidence_stops_and_keeps_initial_proposals(workspace, tmp_path, monkeypatch):
    fake_debate_models(monkeypatch, invalid_reference=True)
    detail = execute(workspace, system(workspace, 'debate'), tmp_path)
    assert detail.run.status == 'failed'
    calls = list(detail.run.final_state['protocol_calls'].values())
    assert len([c for c in calls if c['kind'] == 'propose']) == 2
    assert not any(c['kind'] == 'revise' for c in calls)


def test_debate_disables_peer_evidence_sharing(workspace, tmp_path, monkeypatch):
    fake_debate_models(monkeypatch)
    def patch(spec): spec['stages'][1]['share_evidence'] = False
    detail = execute(workspace, system(workspace, 'debate', patch), tmp_path)
    assert detail.run.status == 'completed'
    for c in detail.run.final_state['protocol_calls'].values():
        if c['kind'] == 'critique':
            assert all('evidence_chunks' not in r['output'] and 'supporting_evidence' not in r['output'] for r in c['input']['reads'])


def test_node_timeout_stops_subsequent_phases(workspace, tmp_path, monkeypatch):
    import time
    def slow(state, **kwargs):
        time.sleep(1.05)
        return {'sub_questions': []}
    monkeypatch.setattr('src.runtime.protocol_runtime.planner_fn', slow)
    def patch(spec): spec['limits']['node_timeout_seconds'] = 1
    detail = execute(workspace, system(workspace, patch=patch), tmp_path)
    assert detail.run.status == 'failed'
    assert '时间上限' in detail.run.error.message
    assert not any(e.node == 'r' for e in detail.events)


def test_protocol_http_sse_history_export_and_store_reload(workspace, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import app
    fake_pipeline(monkeypatch)
    item = system(workspace)
    with TestClient(app) as client:
        response = client.post('/api/v1/qa/stream', json=dict(question='Who held the Chief of Protocol position?', system_id=item['system_id'], draft_revision=1))
        assert response.status_code == 200
        envelopes = [json.loads(s[5:]) for s in response.text.splitlines() if s.startswith('data:')]
        run_id = envelopes[0]['run_id']
        detail = client.get(f'/api/v1/runs/{run_id}').json()
        assert detail['run']['status'] == 'completed'
        assert envelopes[-1]['payload']['result'] == detail['run']['final_state']
        assert len([e for e in envelopes if e['payload'].get('call_id') and 'output' in e['payload']]) == 3
        exported = client.get(f'/api/v1/exports/runs.jsonl?system_id={item["system_id"]}').text
        assert run_id in exported and 'protocol_calls' in exported
    assert SystemStore().get(item['system_id']) == item
    assert ProtocolStore().get(item['draft']['protocol_ref']['protocol_id'])['versions']


def test_protocol_cancellation_preserves_calls_and_has_one_terminal(workspace, tmp_path, monkeypatch):
    from threading import Event, Thread
    import src.runtime.protocol_runtime as runtime
    release, entered = Event(), Event()
    actual = runtime.execute_phase
    def phase(stage, agent_id, *args):
        if stage.kind == 'propose':
            if agent_id == 'b': entered.set(); assert release.wait(10)
            else: assert entered.wait(10)
            return dict(answer=agent_id, evidence_chunks=[])
        return actual(stage, agent_id, *args)
    monkeypatch.setattr(runtime, 'execute_phase', phase)
    item = system(workspace, 'debate')
    context = build_v0_run_context('Q', entrypoint='api', system_id=item['system_id'], draft_revision=1)
    repo = SQLiteRunRepository(tmp_path / 'runs.sqlite3')
    stream = RunService(repo).stream(context)
    while True:
        event = next(stream)
        if event.event_type == 'node_completed' and event.node == 'a': break
    thread = Thread(target=stream.close); thread.start(); release.set(); thread.join(15)
    assert not thread.is_alive()
    detail = repo.get_run_detail(context.run_id)
    assert detail.run.status == 'cancelled'
    assert any(c['agent_id'] == 'a' for c in detail.run.final_state['protocol_calls'].values())
    assert not any(e.payload.get('kind') == 'critique' for e in detail.events)
    assert len([e for e in detail.events if e.event_type in ('run_completed', 'run_failed', 'run_cancelled')]) == 1
