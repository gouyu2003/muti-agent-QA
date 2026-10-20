"""Six executable templates, graph edits, V1 and saved-revision immutability."""
from copy import deepcopy
from threading import Barrier
import pytest
from test_protocol_runtime import workspace, instance, execute, fake_pipeline, fake_lead_models, fake_debate_models, dispatch, reviewed
from src.runtime.protocols import protocol_templates, protocol_templates_v1, parse_protocol, compile_protocol
from src.runtime.protocol_store import digest
from src.runtime.system_store import SystemStore


def create(workspace, template, patch=None, draft=False):
    spec = deepcopy(next(t['spec'] for t in protocol_templates() if t['template_id'] == template))
    if patch: patch(spec)
    protocol = workspace[0].save(dict(name=template, spec=spec))
    release = workspace[0].publish(protocol['protocol_id'], 1)
    agents, bindings = [], {}
    for slot, contract in spec['slots'].items():
        count = max(contract['minimum'], min(2, contract['maximum'])) if contract['role'] == 'expert' else 1
        bindings[slot] = [f'{slot}_{i}' for i in range(count)]
        agents.extend(instance(contract['role'], key) for key in bindings[slot])
    item = workspace[1].create(dict(name=template, agents=agents, protocol_ref=dict(protocol_id=release['protocol_id'],
        **({'revision': 1} if draft else {'version': 'v1'})), protocol_bindings=bindings))
    assert not item['draft']['snapshot']['configuration_errors']
    return item


@pytest.mark.parametrize('template', ['pipeline', 'pipeline_feedback', 'single_expert', 'multi_expert', 'lead', 'debate'])
def test_six_templates_save_publish_and_execute(workspace, tmp_path, monkeypatch, template):
    fake_pipeline(monkeypatch)
    if template == 'lead': fake_lead_models(monkeypatch, [dispatch('experts_0'), reviewed()])
    if template == 'debate': fake_debate_models(monkeypatch)
    item = create(workspace, template)
    frozen = workspace[1].publish(item['system_id'], 1)
    detail = execute(workspace, item, tmp_path)
    assert detail.run.status == 'completed'
    assert detail.run.final_state.get('answer')
    calls = list(detail.run.final_state['protocol_calls'].values())
    assert all(c['status'] == 'completed' for c in calls)
    if template == 'single_expert':
        assert len(calls) == 1 and calls[0]['kind'] == 'answer'
        assert detail.run.final_state['verified'] and detail.run.final_state['supporting_evidence']
    if template == 'multi_expert': assert len([c for c in calls if c['kind'] == 'review']) == 1
    assert workspace[1].version(item['system_id'], 'v1') == frozen


@pytest.mark.parametrize('failure,expected,limit', [('insufficient', 'retrieve', 3), ('conflict', 'plan', 2)])
def test_feedback_routes_count_retries_independently(workspace, tmp_path, monkeypatch, failure, expected, limit):
    def phase(stage, *args):
        if stage.kind == 'plan': return {'sub_questions': [dict(id='sq_1', question='Q', keywords=[], depends_on=[])]}
        if stage.kind == 'retrieve': return {'evidence_chunks': []}
        return dict(answer='', verified=False, failure_type=failure, conflict_flag=failure == 'conflict')
    monkeypatch.setattr('src.runtime.protocol_runtime.execute_phase', phase)
    detail = execute(workspace, create(workspace, 'pipeline_feedback'), tmp_path)
    assert detail.run.status == 'completed'
    routes = [e.payload for e in detail.events if e.event_type == 'route_decided' and e.payload['stage_id'] == 'review']
    assert [r['target'] for r in routes] == [expected] * limit + ['END']
    final = detail.run.final_state
    assert final['retrieval_retry_count'] == (limit if failure == 'insufficient' else 0)
    assert final['plan_retry_count'] == (limit if failure == 'conflict' else 0)


def test_user_structure_edit_changes_real_graph_and_trace(workspace, tmp_path, monkeypatch):
    fake_pipeline(monkeypatch)
    original = create(workspace, 'multi_expert')
    def remove_planner(spec):
        spec['stages'] = [s for s in spec['stages'] if s['kind'] != 'plan']
        del spec['slots']['planner']
        spec['entry_stage'] = 'opinions'
        for s in spec['stages']: s['reads'] = [key for key in s['reads'] if key != 'plan']
    changed = create(workspace, 'multi_expert', remove_planner)
    from src.runtime.context import build_v0_run_context
    from src.runtime.protocol_runtime import build_protocol_graph
    from src.llm_usage import LLMUsageCollector
    def graph(item):
        context = build_v0_run_context('Q', entrypoint='api', system_id=item['system_id'], draft_revision=1)
        return build_protocol_graph(context, LLMUsageCollector()).get_graph()
    assert 'plan' in graph(original).nodes
    assert 'plan' not in graph(changed).nodes
    assert any(e.source == '__start__' and e.target == 'opinions' for e in graph(changed).edges)
    before = execute(workspace, original, tmp_path)
    after = execute(workspace, changed, tmp_path)
    assert before.run.status == after.run.status == 'completed'
    assert original['draft']['snapshot']['workflow_spec']['entry_node'] == 'plan'
    assert changed['draft']['snapshot']['workflow_spec']['entry_node'] == 'opinions'
    assert any(e.payload.get('kind') == 'plan' for e in before.events)
    assert not any(e.payload.get('kind') == 'plan' for e in after.events)
    assert len(after.run.final_state['protocol_calls']) == len(before.run.final_state['protocol_calls']) - 1


def test_parallel_group_joins_once_with_independent_inputs(workspace, tmp_path, monkeypatch):
    from test_protocol_runtime import fake_lead_models
    fake_pipeline(monkeypatch)
    seen = fake_lead_models(monkeypatch, [], Barrier(2))
    def parallel(spec): spec['stages'][1]['mode'] = 'parallel'
    detail = execute(workspace, create(workspace, 'multi_expert', parallel), tmp_path)
    assert detail.run.status == 'completed'
    assert [key for key, _ in seen].count('reviewer_0') == 1
    for key, prompt in seen:
        if key.startswith('experts'): assert '各专家独立观点' not in prompt


def test_saved_draft_reference_does_not_follow_edits_publish_or_archive(workspace, tmp_path, monkeypatch):
    fake_pipeline(monkeypatch)
    item = create(workspace, 'single_expert', draft=True)
    first_run = execute(workspace, item, tmp_path)
    before = deepcopy(workspace[1].run_snapshot(item['system_id'], revision=1))
    key = item['draft']['protocol_ref']['protocol_id']
    doc = workspace[0].get(key)['draft']
    doc['spec']['stages'][0]['instructions'] = 'Changed later'
    workspace[0].save(doc, key, 1)
    workspace[0].publish(key, 2); workspace[0].archive(key, 2)
    assert workspace[1].run_snapshot(item['system_id'], revision=1) == before
    second_run = execute(workspace, item, tmp_path)
    assert first_run.run.status == second_run.run.status == 'completed'
    assert first_run.run.final_state['answer'] == second_run.run.final_state['answer']
    assert first_run.run.context.config_snapshot['config_sha256'] == second_run.run.context.config_snapshot['config_sha256']
    fields = {k: deepcopy(v) for k, v in item['draft'].items() if k != 'snapshot'}
    fields['name'] = 'Rename only'
    saved = workspace[1].save(item['system_id'], 1, fields)
    assert saved['draft']['snapshot']['protocol_document'] == before['protocol_document']
    assert saved['draft']['protocol_bindings'] == before['protocol_bindings']


def test_v1_normalization_compiler_and_snapshot_remain_identical(workspace):
    # V2-only defaults must not enter the V1 normalized spec or compiled snapshot.
    raw = protocol_templates_v1()[0]['spec']
    agents = [instance(role, role) for role in ('planner', 'retriever', 'answer_validator')]
    bindings = dict(planner=['planner'], retriever=['retriever'], reviewer=['answer_validator'])
    expected = dict(compiler_version='1', spec=deepcopy(raw), bindings=bindings)
    assert compile_protocol(raw, bindings, agents) == expected
    assert 'retry_limits' not in parse_protocol(raw).model_dump()
    protocol = workspace[0].save(dict(name='V1', spec=raw))
    workspace[0].publish(protocol['protocol_id'], 1)
    item = workspace[1].create(dict(name='V1 system', agents=agents, protocol_ref=dict(protocol_id=protocol['protocol_id'], version='v1'), protocol_bindings=bindings))
    frozen = workspace[1].publish(item['system_id'], 1)
    checksum = digest(frozen)
    assert digest(SystemStore().version(item['system_id'], 'v1')) == checksum


def test_feedback_mixed_failures_share_no_retry_counter(workspace, tmp_path, monkeypatch):
    failures = iter(['insufficient', 'conflict', 'insufficient', 'conflict', 'insufficient', 'insufficient'])
    def phase(stage, *args):
        if stage.kind == 'plan': return {'sub_questions': [dict(id='sq_1', question='Q', keywords=[], depends_on=[])]}
        if stage.kind == 'retrieve': return {'evidence_chunks': []}
        failure = next(failures)
        return dict(answer='', verified=False, failure_type=failure, conflict_flag=failure == 'conflict')
    monkeypatch.setattr('src.runtime.protocol_runtime.execute_phase', phase)
    detail = execute(workspace, create(workspace, 'pipeline_feedback'), tmp_path)
    assert detail.run.status == 'completed'
    assert detail.run.final_state['retrieval_retry_count'] == 3
    assert detail.run.final_state['plan_retry_count'] == 2
    assert len(detail.run.final_state['protocol_calls']) == 15


def test_configured_feedback_edges_are_real_compiled_graph_edges(workspace):
    from src.runtime.context import build_v0_run_context
    from src.runtime.protocol_runtime import build_protocol_graph
    from src.llm_usage import LLMUsageCollector
    fixed = create(workspace, 'pipeline')
    feedback = create(workspace, 'pipeline_feedback')
    def edges(item):
        context = build_v0_run_context('Q', entrypoint='api', system_id=item['system_id'], draft_revision=1)
        return {(e.source, e.target) for e in build_protocol_graph(context, LLMUsageCollector()).get_graph().edges}
    assert ('review', 'retrieve') not in edges(fixed)
    assert ('review', 'plan') not in edges(fixed)
    assert {('review', 'retrieve'), ('review', 'plan')} <= edges(feedback)


@pytest.mark.parametrize('template', ['pipeline', 'lead', 'debate'])
def test_v1_protocols_still_execute_without_converting_snapshots(workspace, tmp_path, monkeypatch, template):
    fake_pipeline(monkeypatch)
    if template == 'lead': fake_lead_models(monkeypatch, [dispatch('experts_0'), reviewed()])
    if template == 'debate': fake_debate_models(monkeypatch)
    def old_version(spec):
        spec.clear()
        spec.update(deepcopy(next(t['spec'] for t in protocol_templates_v1() if t['template_id'] == template)))
    item = create(workspace, template, old_version)
    before = deepcopy(item['draft'])
    detail = execute(workspace, item, tmp_path)
    assert detail.run.status == 'completed' and detail.run.final_state['answer']
    assert workspace[1].get(item['system_id'])['draft'] == before
    assert before['snapshot']['protocol_plan']['compiler_version'] == '1'
    assert 'retry_limits' not in before['snapshot']['protocol_document']['spec']
