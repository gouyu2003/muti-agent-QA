"""Lead request, repair and persisted-failure regressions without live model calls."""
from copy import deepcopy
import json

import pytest

from src.agents.structured_output import invoke_json
from src.llm_usage import LLMUsageCollector
from src.runtime.protocol_agents import LeadDecision, lead_decision
from src.runtime.protocol_runtime import ProtocolExecutionError
from test_structured_output import Chat, message
from test_protocol_runtime import (
    workspace, system, execute, fake_lead_models, dispatch, reviewed,
    build_v0_run_context, RunService, SQLiteRunRepository,
)


def lead_input(reads=None):
    return dict(question='What is the catalog number of Silver Lantern Atlas?',
        task={'instruction': 'Choose only relevant experts.'}, reads=reads or [],
        candidates=[{'agent_id': key, 'name': name, 'description': description} for key, name, description in [
            ('founder', 'Founder Expert', 'Institution founders'),
            ('location', 'Location Expert', 'Institution cities'),
            ('catalog', 'Catalog Expert', 'Book catalog numbers')]])


def config():
    # Represents an existing saved prompt without any JSON instruction.
    return {'model_config': {'system_prompt': 'Coordinate evidence-grounded experts.', 'max_tokens': 4096}}


def run_lead(monkeypatch, responses, reads=None):
    chat = Chat([message(json.dumps(response)) for response in responses])
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda _: chat)
    usage = LLMUsageCollector()
    result = lead_decision(config(), lead_input(reads), usage)
    return result, chat, usage


def test_existing_prompt_gets_json_schema_and_registered_ids(monkeypatch):
    result, chat, _ = run_lead(monkeypatch, [dispatch('catalog')])
    assert result['tasks'][0]['agent_id'] == 'catalog'
    assert 'JSON' in chat.prompts[0]
    request = json.loads(chat.prompts[0].split('\n', 1)[1])
    schema = request['output_contract']
    assert schema['additionalProperties'] is False
    assert schema['$defs']['Assignment']['additionalProperties'] is False
    assert schema['$defs']['Assignment']['properties']['agent_id']['enum'] == ['catalog', 'founder', 'location']
    assert request['allowed_actions'] == ['dispatch']
    for example in request['format_examples'].values():
        LeadDecision.model_validate(example)


@pytest.mark.parametrize('bad,path,code', [
    ({**dispatch('catalog'), 'reason': None}, 'reason', 'string_type'),
    ({**dispatch('catalog'), 'extra': 'PRIVATE MODEL OUTPUT'}, 'extra', 'extra_forbidden'),
    ({**dispatch('catalog'), 'action': 'dispatch or review'}, 'action', 'literal_error'),
    (dispatch('Catalog Expert'), 'tasks.0.agent_id', 'string_pattern_mismatch'),
    (dispatch('unregistered'), 'tasks.0.agent_id', 'unregistered_agent'),
    (reviewed(), 'action', 'review_before_dispatch'),
    ({**dispatch('catalog'), 'tasks': []}, 'tasks', 'action_tasks_mismatch'),
    ({**dispatch('catalog'), 'tasks': [dict(task_id='1', agent_id='catalog', instruction='Find number')]},
        'tasks.0.task_id', 'string_pattern_mismatch'),
    ({**dispatch('catalog'), 'tasks': dispatch('catalog')['tasks'] * 2}, 'tasks', 'duplicate_task_id'),
    (dispatch('catalog', instruction='   '), 'tasks', 'blank_instruction'),
])
def test_specific_errors_are_fed_back_and_repaired(monkeypatch, bad, path, code):
    result, chat, usage = run_lead(monkeypatch, [bad, dispatch('catalog')])
    assert result['action'] == 'dispatch'
    assert usage.snapshot().call_count == 2
    issue = usage.attempts()[0]['validation_errors'][0]
    assert issue['path'] == path and issue['code'] == code
    assert code in chat.prompts[1] and issue['message'] in chat.prompts[1]
    assert 'PRIVATE MODEL OUTPUT' not in json.dumps(usage.attempts())
    assert 'PRIVATE MODEL OUTPUT' not in chat.prompts[1]


def test_review_requires_nonblank_summary_after_expert_results(monkeypatch):
    result, chat, usage = run_lead(monkeypatch, [{**reviewed(), 'summary': '  '}, reviewed()], [{'kind': 'dispatch'}])
    assert result['action'] == 'review'
    assert usage.attempts()[0]['validation_errors'][0]['code'] == 'missing_summary'
    request = json.loads(chat.prompts[0].split('\n', 1)[1])
    assert request['allowed_actions'] == ['dispatch', 'review']
    for example in request['format_examples'].values():
        LeadDecision.model_validate(example)


def test_exhaustion_remains_bounded_and_reports_specific_failure(monkeypatch):
    chat = Chat([message(json.dumps(dispatch('unknown')))] * 3)
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda _: chat)
    usage = LLMUsageCollector()
    with pytest.raises(ProtocolExecutionError, match=r'tasks.0.agent_id.*目标专家未注册'):
        lead_decision(config(), lead_input(), usage)
    assert usage.snapshot().call_count == 3
    assert all(c['outcome'] == 'invalid_schema' for c in usage.attempts())


def test_generic_validation_exceptions_do_not_leak_raw_error_text():
    def reject(_):
        raise ValueError('PRIVATE EXCEPTION CONTENT')
    chat = Chat([message('{"reason":"PRIVATE MODEL OUTPUT"}')] * 2)
    _, diagnostics = invoke_json(chat, 'JSON', 1, validate=reject)
    assert 'PRIVATE' not in json.dumps(diagnostics)
    assert 'PRIVATE' not in chat.prompts[1]


def test_failure_details_survive_history_reload(workspace, tmp_path, monkeypatch):
    seen = fake_lead_models(monkeypatch, [dispatch('unknown')])
    detail = execute(workspace, system(workspace, 'lead'), tmp_path)
    loaded = SQLiteRunRepository(tmp_path / 'runs.sqlite3').get_run_detail(detail.run.context.run_id)
    assert loaded.run.status == 'failed'
    assert 'tasks.0.agent_id' in loaded.run.error.message
    assert len(seen) == 3 and {key for key, _ in seen} == {'boss'}
    attempts = loaded.run.metrics.agent_usage['boss']['calls']
    assert len(attempts) == 3
    assert all(c['validation_errors'][0]['code'] == 'unregistered_agent' for c in attempts)
    call = next(iter(loaded.run.final_state['protocol_calls'].values()))
    assert '目标专家未注册' in call['output']['error']


def test_published_system_repairs_then_dispatches_and_reviews_without_editing_snapshot(workspace, tmp_path, monkeypatch):
    seen = fake_lead_models(monkeypatch, [reviewed(), dispatch('a'), reviewed()])
    item = system(workspace, 'lead')
    frozen = deepcopy(workspace[1].publish(item['system_id'], 1))
    context = build_v0_run_context('Who held the Chief of Protocol position?', entrypoint='api',
        system_id=item['system_id'], system_version='v1')
    detail = RunService(SQLiteRunRepository(tmp_path / 'runs.sqlite3')).run(context)
    assert detail.run.status == 'completed'
    assert [key for key, _ in seen] == ['boss', 'boss', 'a', 'boss', 'v']
    assert 'review_before_dispatch' in seen[1][1]
    assert workspace[1].version(item['system_id'], 'v1') == frozen
    calls = list(detail.run.final_state['protocol_calls'].values())
    assert [c['agent_id'] for c in calls if c['kind'] == 'dispatch'] == ['a']
