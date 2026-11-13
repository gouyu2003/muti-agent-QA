"""Conversation lifecycle, isolation and context use without provider requests."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from src.llm_usage import LLMUsageCollector
from src.runtime.chat_context import bounded_history, context_config, resolve_context
from src.runtime.chat_store import ChatStore
from src.runtime.interactive import InteractiveRuns, ExecutionFailure
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.system_store import SystemStore


class AnswerGraph:
    def __init__(self, questions):
        self.questions = questions

    def stream(self, state, **kwargs):
        self.questions.append(state['question'])
        yield {'type': 'updates', 'data': {'answer_validator_v0': {
            'answer': '研究生可借 30 册。', 'verified': True, 'supporting_evidence': []}}}


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv('MAQA_SYSTEMS_DIR', str(tmp_path / 'systems'))
    monkeypatch.setenv('MAQA_KNOWLEDGE_DIR', str(tmp_path / 'knowledge'))
    monkeypatch.setenv('MAQA_DISABLE_MODEL_CALLS', '1')
    class RoutingModel:
        def invoke(self, prompt):
            data = json.loads(prompt.split('\n输入数据：\n', 1)[1])
            question = data['current_question']
            return SimpleNamespace(content=json.dumps({'intent': 'standalone', 'resolved_question': question,
                'knowledge_question': question, 'general_question': '', 'clarification': '', 'used_message_ids': []}),
                usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
    monkeypatch.setattr('src.llm.build_chat_model', lambda config: RoutingModel())
    questions = []
    repository = SQLiteRunRepository(tmp_path / 'runs.sqlite3')
    service = RunService(repository, graph_factory=lambda usage: AnswerGraph(questions))
    manager = InteractiveRuns(repository, launcher=lambda *args: None)
    systems = SystemStore()
    system = systems.create({'name': '图书馆', 'description': '', 'workflow_id': 'multi_hop_qa_v0',
                             'source_id': 'hotpotqa_fixture', 'source_version': None})
    store = ChatStore(repository, manager, systems)
    store.initialize()
    return SimpleNamespace(store=store, repository=repository, service=service, manager=manager,
                           systems=systems, system=system, questions=questions)


def create(chat, key='create-1'):
    return chat.store.create({'system_id': chat.system['system_id'], 'draft_revision': 1}, key)


def finish(chat, submitted):
    context = chat.repository.get_run(submitted['run_id']).context
    return chat.service.run(context, prepared=True)


def test_create_freeze_idempotence_restart_and_delete_preserves_run(chat):
    conversation = create(chat)
    key = conversation['conversation_id']
    assert create(chat)['conversation_id'] == key
    snapshot = deepcopy(conversation['config_snapshot'])
    chat.systems.save(chat.system['system_id'], 1, {'name': '另一个名称', 'description': '',
        'workflow_id': 'multi_hop_qa_v0', 'source_id': 'hotpotqa_fixture', 'source_version': None})
    submitted = chat.store.send(key, '研究生能借多少本书？', 'one')
    assert chat.repository.get_run(submitted['run_id']).context.config_snapshot['system_ref']['draft_revision'] == 1
    with pytest.raises(ExecutionFailure, match='停止'):
        chat.store.delete(key)
    finish(chat, submitted)
    assert chat.questions == ['研究生能借多少本书？']
    restarted = ChatStore(SQLiteRunRepository(chat.repository.db_path), chat.manager, chat.systems)
    assert restarted.get(key)['config_snapshot'] == snapshot
    assert [m['status'] for m in restarted.messages(key)] == ['completed', 'completed']
    assert restarted.messages(key)[1]['content'] == '研究生可借 30 册。'
    restarted.rename(key, '借阅咨询')
    assert restarted.list()[0]['title'] == '借阅咨询'
    restarted.delete(key)
    assert restarted.list() == []
    assert chat.repository.get_run(submitted['run_id']).status == 'completed'


def test_duplicate_busy_and_transaction_rollback(chat):
    a, b = create(chat), create(chat, 'create-2')
    submitted = chat.store.send(a['conversation_id'], '研究生借阅', 'one')
    again = chat.store.send(a['conversation_id'], '研究生借阅', 'one')
    assert again['run_id'] == submitted['run_id']
    with pytest.raises(ExecutionFailure):
        chat.store.send(a['conversation_id'], '改变问题', 'one')
    with pytest.raises(ExecutionFailure) as error:
        chat.store.send(b['conversation_id'], '另一个问题', 'two')
    assert error.value.active_run_id == submitted['run_id']
    assert chat.store.messages(b['conversation_id']) == []
    finish(chat, submitted)
    original = chat.repository.create_run
    def broken(record):
        original(record)
        raise RuntimeError('injected reservation failure')
    chat.repository.create_run = broken
    with pytest.raises(RuntimeError):
        chat.store.send(b['conversation_id'], '问题', 'three')
    assert chat.store.messages(b['conversation_id']) == []
    with chat.repository._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1


def mock_resolution(monkeypatch, *, intent='followup', question='本科生最多可借几册图书？', clarification=''):
    class Model:
        def invoke(self, prompt):
            return SimpleNamespace(content=json.dumps({'intent': intent, 'resolved_question': question,
                'knowledge_question': question if intent != 'clarification' else '', 'general_question': '',
                'clarification': clarification, 'used_message_ids': []}), usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
    monkeypatch.setattr('src.llm.build_chat_model', lambda config: Model())


def test_followup_uses_resolved_question_and_tracks_usage(chat, monkeypatch):
    key = create(chat)['conversation_id']
    finish(chat, chat.store.send(key, '研究生能借多少本书？', 'one'))
    mock_resolution(monkeypatch)
    submitted = chat.store.send(key, '那本科生呢？', 'two')
    detail = finish(chat, submitted)
    assert chat.questions[-1] == '本科生最多可借几册图书？'
    assert detail.run.context.question == '那本科生呢？'
    assert detail.run.metrics.llm_call_count == 1
    assert detail.run.metrics.agent_usage['chat_context']['total_tokens'] == 15
    assert chat.store.messages(key)[-1]['context']['resolved_question'] == chat.questions[-1]
    other = create(chat, 'create-2')['conversation_id']
    separate = chat.store.send(other, '独立问题', 'one')
    assert chat.repository.get_run(separate['run_id']).context.config_snapshot['_chat']['history'] == []


def test_clarification_and_formatting_and_bad_json(chat, monkeypatch):
    key = create(chat)['conversation_id']
    finish(chat, chat.store.send(key, '研究生借阅', 'one'))
    mock_resolution(monkeypatch, intent='clarification', question='', clarification='你指的是哪类读者？')
    detail = finish(chat, chat.store.send(key, '那他们呢？', 'two'))
    assert len(chat.questions) == 1
    assert detail.run.final_state['answer'] == '你指的是哪类读者？'
    assert detail.run.final_state['verified'] is False
    mock_resolution(monkeypatch, intent='formatting', question='请用表格列出研究生图书借阅规则。')
    detail = finish(chat, chat.store.send(key, '整理成表格', 'three'))
    assert detail.run.final_state['chat_context']['intent'] == 'formatting'
    monkeypatch.setattr('src.llm.build_chat_model', lambda config: SimpleNamespace(invoke=lambda prompt: SimpleNamespace(content='broken json')))
    detail = finish(chat, chat.store.send(key, '那老师呢', 'four'))
    assert detail.run.status == 'failed'
    assert detail.run.error.error_type == 'chat_context_error'
    assert len(chat.questions) == 2  # no raw ambiguous-question fallback


def test_cancel_and_interrupt_sync_messages(chat):
    key = create(chat)['conversation_id']
    submitted = chat.store.send(key, '问题', 'one')
    chat.manager.settle(submitted['run_id'], 'cancelled', 'cancelled', '已取消')
    assert all(m['status'] == 'cancelled' for m in chat.store.messages(key))
    submitted = chat.store.send(key, '新问题', 'two')
    assert chat.repository.get_run(submitted['run_id']).context.config_snapshot['_chat']['history'] == []
    with chat.repository._connection() as db:
        db.execute("UPDATE interactive_runs SET created_at='2020-01-01T00:00:00+00:00' WHERE run_id=?", (submitted['run_id'],))
    chat.manager.reconcile_all()
    assert chat.store.messages(key)[-1]['status'] == 'interrupted'


def test_history_window_and_model_selection():
    messages = []
    for turn in range(1, 6):
        for role in ('user', 'assistant'):
            messages.append({'turn_index': turn, 'status': 'completed', 'role': role, 'content': 'hello',
                             'message_id': f'{turn}-{role}', 'context': {}})
    assert {m['turn_index'] for m in bounded_history(messages)} == {3, 4, 5}
    messages[-1]['status'] = 'failed'
    assert {m['turn_index'] for m in bounded_history(messages)} == {2, 3, 4}
    for role in ('planner', 'lead', 'answer_validator', 'expert'):
        config = context_config({'agents': {'agent': {'role': role, 'model_config': {'model_name': 'test'}}}})
        assert config['agent_id'] == 'agent' and config['model_config']['max_tokens'] == 1024
        assert config['general_model_config']['max_tokens'] == 2048
    result = resolve_context({'history': [], 'has_history': True, 'config': {'prompt_version': 'v1'}},
                             '那他们呢？', LLMUsageCollector())
    assert result['intent'] == 'clarification'


def test_cancel_before_context_never_calls_model(chat, monkeypatch):
    from threading import Event
    key = create(chat)['conversation_id']
    finish(chat, chat.store.send(key, '研究生借阅', 'one'))
    monkeypatch.setattr('src.llm.build_chat_model', lambda config: pytest.fail('cancelled request must not call a model'))
    submitted = chat.store.send(key, '那本科生呢', 'two')
    cancelled = Event()
    cancelled.set()
    detail = chat.service.run(chat.repository.get_run(submitted['run_id']).context, prepared=True, cancellation=cancelled)
    assert detail.run.status == 'cancelled'
    assert detail.run.metrics.llm_call_count == 0


def test_api_contract(chat, monkeypatch):
    monkeypatch.setattr(app.state, 'run_service_factory', lambda: chat.service)
    with TestClient(app) as client:
        app.state.chat.interactive.launcher = lambda *args: None
        payload = {'system_id': chat.system['system_id'], 'draft_revision': 1, 'client_request_id': 'api-create'}
        result = client.post('/api/v1/conversations', json=payload)
        assert result.status_code == 201
        key = result.json()['conversation_id']
        assert client.post('/api/v1/conversations', json=payload).json()['conversation_id'] == key
        sent = client.post(f'/api/v1/conversations/{key}/messages', json={'question': '问题', 'client_request_id': 'api-one'})
        assert sent.status_code == 202
        assert client.get(f'/api/v1/conversations/{key}/messages').json()['items'][0]['content'] == '问题'
        assert client.delete(f'/api/v1/conversations/{key}').status_code == 409
        app.state.interactive.settle(sent.json()['run_id'], 'cancelled', 'cancelled', '已取消')
        assert client.patch(f'/api/v1/conversations/{key}', json={'title': '新标题'}).status_code == 200
        assert client.delete(f'/api/v1/conversations/{key}').status_code == 204
        assert client.get(f'/api/v1/conversations/{key}').status_code == 404


@pytest.mark.parametrize('family', ['single_expert', 'multi_expert', 'protocol_pipeline'])
def test_chat_runs_saved_instance_and_protocol_systems(chat, monkeypatch, tmp_path, family):
    from src.runtime.instances import templates
    from src.runtime.protocols import protocol_templates
    from src.runtime.protocol_store import ProtocolStore
    monkeypatch.setenv('MAQA_PROTOCOLS_DIR', str(tmp_path / 'protocols'))
    def instance(role, key):
        value = deepcopy(next(t['defaults'] for t in templates() if t['agent_type'] == role))
        value.update(agent_id=key, name=key)
        if role in ('expert', 'retriever'):
            value.update(source_id='hotpotqa_fixture', source_version=None)
        return value
    if family == 'protocol_pipeline':
        spec = next(t['spec'] for t in protocol_templates() if t['template_id'] == 'pipeline')
        store = ProtocolStore()
        protocol = store.save({'name': 'Test protocol', 'spec': spec})
        store.publish(protocol['protocol_id'], 1)
        agents = [instance(role, role) for role in ('planner', 'retriever', 'answer_validator')]
        fields = {'name': 'Protocol chat', 'agents': agents,
            'protocol_ref': {'protocol_id': protocol['protocol_id'], 'version': 'v1'},
            'protocol_bindings': {slot: [a['agent_id'] for a in agents if a['agent_type'] == contract['role']]
                                  for slot, contract in spec['slots'].items()}}
    else:
        agents = [instance('expert', 'expert')]
        if family == 'multi_expert':
            agents += [instance('expert', 'expert2'), instance('answer_validator', 'review')]
        fields = {'name': 'Expert chat', 'agents': agents, 'workflow_id': family, 'execution_mode': 'sequential'}
    item = chat.systems.create(fields)
    output = lambda state, **kwargs: {'answer': state['question'], 'verified': True, 'failure_type': 'none', 'supporting_evidence': []}
    for module in ('src.runtime.nodes', 'src.runtime.protocol_runtime'):
        monkeypatch.setattr(module + '.answer_validator_fn', output)
        monkeypatch.setattr(module + '.planner_fn', lambda state, **kwargs: {'sub_questions': [
            {'id': 'sq_1', 'question': state['question'], 'keywords': [], 'depends_on': []}], 'retrieval_strategy': 'sequential'})
    real_service = RunService(chat.repository)
    key = chat.store.create({'system_id': item['system_id'], 'draft_revision': 1}, family)['conversation_id']
    for request, question in [('first', 'Who held the Chief of Protocol position?'), ('second', '那本科生呢？')]:
        if request == 'second':
            mock_resolution(monkeypatch)
        submitted = chat.store.send(key, question, request)
        detail = real_service.run(chat.repository.get_run(submitted['run_id']).context, prepared=True)
        assert detail.run.status == 'completed'
    assert chat.store.messages(key)[-1]['context']['resolved_question'] == '本科生最多可借几册图书？'


def test_concurrent_duplicate_reserves_only_one_run(chat):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    key = create(chat)['conversation_id']
    barrier = Barrier(2)
    def submit():
        barrier.wait()
        try:
            return chat.store.send(key, '并发问题', 'same-request')
        except ExecutionFailure:
            return None  # caller retries the same id after another admission releases its lock
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert any(results)
    assert len(chat.store.messages(key)) == 2
    with chat.repository._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1


def hybrid_model(monkeypatch, *, knowledge='', general='', answer='你好！有什么可以帮你？',
                 intent='standalone', clarification='', general_outputs=None):
    """Provider stub with explicit routing decisions, not a keyword classifier."""
    calls = []
    outputs = iter(general_outputs) if general_outputs is not None else None
    class Model:
        def __init__(self, config):
            self.general = config['model_config']['max_tokens'] == 2048

        def invoke(self, prompt):
            calls.append((self.general, prompt))
            if self.general:
                value = next(outputs) if outputs is not None else {'answer': answer}
                if isinstance(value, Exception):
                    raise value
            else:
                value = {'intent': intent, 'resolved_question': '' if clarification else knowledge or general,
                         'knowledge_question': knowledge, 'general_question': general,
                         'clarification': clarification, 'used_message_ids': []}
            return SimpleNamespace(content=value if isinstance(value, str) else json.dumps(value),
                usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
    monkeypatch.setattr('src.llm.build_chat_model', Model)
    return calls


@pytest.mark.parametrize('question', ['你好', '你能做什么？', '解释机器学习', '写一封感谢信', '写一个 Python 排序函数'])
def test_general_first_turn_skips_knowledge_and_persists_metadata(chat, monkeypatch, question):
    hybrid_model(monkeypatch, general=question, answer='**通用回复**')
    key = create(chat)['conversation_id']
    detail = finish(chat, chat.store.send(key, question, 'one'))
    state = detail.run.final_state
    assert detail.run.status == 'completed'
    assert chat.questions == []
    assert state['answer'] == '**通用回复**'
    assert state['response_parts'] == [{'kind': 'general', 'content': '**通用回复**'}]
    assert state['verification_applicable'] is False and state['verified'] is False
    assert state['failure_type'] == 'none' and state['supporting_evidence'] == []
    assert detail.run.metrics.llm_call_count == 2
    assert set(detail.run.metrics.agent_usage) == {'chat_context', 'chat_general'}
    assert chat.store.messages(key)[-1]['result'] == state
    terminal = detail.events[-1].payload['result']
    exported = json.loads(next(chat.repository.export_runs_jsonl()))['run']['final_state']
    assert terminal == exported == state
    summary = chat.repository.get_stats_summary()
    assert summary.completed_runs == 1 and summary.verification_applicable_runs == 0
    assert summary.verification_pass_rate == 0


@pytest.mark.parametrize('failure', ['none', 'insufficient', 'conflict'])
def test_mixed_keeps_verdict_and_only_shares_confirmed_knowledge(chat, monkeypatch, failure):
    calls = hybrid_model(monkeypatch, knowledge='研究生能借几本？', general='给一些阅读建议', answer='建议定期阅读。')
    class KnowledgeGraph:
        def stream(self, state, **kwargs):
            yield {'type': 'updates', 'data': {'answer_validator_v0': {
                'answer': '30 册' if failure == 'none' else '未经证实的错误政策 999 册',
                'verified': failure == 'none', 'failure_type': failure,
                'verification_note': '原始验证说明', 'supporting_evidence': []}}}
    chat.service.graph_factory = lambda usage: KnowledgeGraph()
    key = create(chat)['conversation_id']
    detail = finish(chat, chat.store.send(key, '借阅规则和阅读建议', 'one'))
    state = detail.run.final_state
    assert detail.run.status == 'completed'
    assert [p['kind'] for p in state['response_parts']] == ['knowledge', 'general']
    assert state['verified'] is (failure == 'none')
    assert state['verification_applicable'] is True
    assert state['verification_note'] == '原始验证说明'
    assert '999' not in state['answer'] and '999' not in calls[-1][1]
    assert '通用部分' in state['answer'] and '建议定期阅读。' in state['answer']
    assert any(e.payload.get('output', {}).get('answer', '').startswith('未经证实')
               for e in detail.events) is (failure != 'none')
    if failure == 'conflict':
        assert '冲突' in state['response_parts'][0]['content']
    if failure == 'insufficient':
        assert '没有找到' in state['response_parts'][0]['content']


def test_mixed_general_failure_preserves_knowledge_and_records_failure(chat, monkeypatch):
    hybrid_model(monkeypatch, knowledge='研究生借阅', general='阅读建议', general_outputs=['invalid', {'answer': ''}])
    key = create(chat)['conversation_id']
    detail = finish(chat, chat.store.send(key, '混合请求', 'one'))
    assert detail.run.status == 'failed'
    assert detail.run.error.error_type == 'chat_response_error'
    assert detail.run.metrics.agent_usage['chat_general']['call_count'] == 2
    assert detail.run.final_state['response_parts'] == [{'kind': 'knowledge', 'content': '研究生可借 30 册。'}]
    assert chat.store.messages(key)[-1]['content'] == '研究生可借 30 册。'
    assert chat.store.messages(key)[-1]['error']
    assert len(chat.questions) == 1


def test_routing_repairs_once_without_changing_to_another_path(chat, monkeypatch):
    calls = []
    class InvalidModel:
        def invoke(self, prompt):
            calls.append(prompt)
            return SimpleNamespace(content='{}')
    monkeypatch.setattr('src.llm.build_chat_model', lambda config: InvalidModel())
    detail = finish(chat, chat.store.send(create(chat)['conversation_id'], '完整问题', 'one'))
    assert detail.run.status == 'failed' and detail.run.error.error_type == 'chat_context_error'
    assert len(calls) == 2 and chat.questions == []


def test_general_repair_and_cancel_during_generation(chat, monkeypatch):
    hybrid_model(monkeypatch, general='你好', general_outputs=[{'answer': ''}, {'answer': '你好！'}])
    key = create(chat)['conversation_id']
    detail = finish(chat, chat.store.send(key, '你好', 'one'))
    assert detail.run.status == 'completed'
    assert detail.run.metrics.llm_call_count == 3
    assert detail.run.final_state['chat_context']['general_diagnostics']['retry_count'] == 1
    from threading import Event
    cancellation = Event()
    def cancelled(*args):
        cancellation.set()
        args[-1].check_cancelled()
    hybrid_model(monkeypatch, knowledge='研究生借阅', general='阅读建议')
    monkeypatch.setattr('src.runtime.chat_response.general_response', cancelled)
    submitted = chat.store.send(key, '混合请求', 'two')
    detail = chat.service.run(chat.repository.get_run(submitted['run_id']).context,
                            prepared=True, cancellation=cancellation)
    assert detail.run.status == 'cancelled'
    assert detail.run.final_state['response_parts'][0]['kind'] == 'knowledge'
    assert chat.store.messages(key)[-1]['status'] == 'cancelled'


def test_clarification_and_complete_question_when_history_unavailable(chat, monkeypatch):
    config = context_config(create(chat)['config_snapshot'])
    hybrid_model(monkeypatch, intent='clarification', clarification='请说明是哪类读者。')
    result = resolve_context({'history': [], 'has_history': True, 'config': config}, '那他们呢？', LLMUsageCollector())
    assert result['intent'] == 'clarification'
    hybrid_model(monkeypatch, general='解释机器学习')
    result = resolve_context({'history': [], 'has_history': True, 'config': config}, '解释机器学习', LLMUsageCollector())
    assert result['general_question'] == '解释机器学习'
    hybrid_model(monkeypatch, intent='clarification', clarification='请说明是哪类读者。')
    detail = finish(chat, chat.store.send(create(chat, 'another')['conversation_id'], '那他们呢？', 'one'))
    assert detail.run.status == 'completed' and detail.run.final_state['verification_applicable'] is False
    assert chat.questions == [] and detail.run.metrics.llm_call_count == 1


def test_legacy_queued_run_uses_frozen_policy_and_new_send_upgrades(chat, monkeypatch):
    from src.runtime.chat_context import LEGACY_INSTRUCTIONS, POLICY_VERSION
    key = create(chat)['conversation_id']
    snapshot = deepcopy(chat.store.get(key)['config_snapshot'])
    legacy = context_config(snapshot)
    for field in ('policy_version', 'general_model_config', 'general_instructions', 'system_scope'):
        legacy.pop(field)
    legacy.update(instructions=LEGACY_INSTRUCTIONS, prompt_version='chat-context-v1')
    legacy['model_config'].update(system_prompt=LEGACY_INSTRUCTIONS, max_tokens=512)
    with chat.repository._connection() as db:
        db.execute('UPDATE conversations SET context_config_json=? WHERE conversation_id=?', (json.dumps(legacy), key))
    queued = chat.store.send(key, '研究生借阅', 'queued')
    record = chat.repository.get_run(queued['run_id'])
    record.context.config_snapshot['_chat']['config'] = legacy
    # Simulate a Run admitted by the old release, without rewriting its system snapshot.
    with chat.repository._connection() as db:
        db.execute('UPDATE runs SET config_snapshot_json=? WHERE run_id=?',
                   (json.dumps(record.context.config_snapshot), queued['run_id']))
    monkeypatch.setattr('src.llm.build_chat_model', lambda config: pytest.fail('legacy first turn must skip the model'))
    original = finish(chat, queued)
    assert original.run.status == 'completed' and original.run.metrics.llm_call_count == 0
    hybrid_model(monkeypatch, general='你好')
    submitted = chat.store.send(key, '你好', 'new')
    detail = finish(chat, submitted)
    assert detail.run.context.config_snapshot['_chat']['config']['policy_version'] == POLICY_VERSION
    assert chat.store.get(key)['config_snapshot'] == snapshot
    assert chat.store.get(key)['context_config'] == legacy
    assert chat.store.get(key)['chat_policy_version'] == POLICY_VERSION
    assert chat.store.send(key, '研究生借阅', 'queued')['run_id'] == queued['run_id']
    assert chat.repository.get_run(queued['run_id']).context == original.run.context


def test_history_includes_general_exchange_for_followup_without_relabeling_evidence(chat, monkeypatch):
    key = create(chat)['conversation_id']
    finish(chat, chat.store.send(key, '研究生借阅', 'one'))
    hybrid_model(monkeypatch, general='你好', answer='你好！')
    finish(chat, chat.store.send(key, '你好', 'two'))
    calls = hybrid_model(monkeypatch, knowledge='本科生最多借几册？', intent='followup')
    finish(chat, chat.store.send(key, '那本科生呢？', 'three'))
    assert chat.questions == ['研究生借阅', '本科生最多借几册？']
    assert '研究生借阅' in calls[0][1] and '你好！' in calls[0][1]
    assert chat.store.messages(key)[-3]['result']['verification_applicable'] is False


def test_statistics_eligibility_consistent_for_legacy_general_mixed_and_clarification(chat, monkeypatch):
    from src.runtime.history import summarize_history
    key = create(chat)['conversation_id']
    finish(chat, chat.store.send(key, '研究生借阅', 'knowledge'))
    hybrid_model(monkeypatch, general='你好')
    finish(chat, chat.store.send(key, '你好', 'general'))
    only = chat.repository.get_stats_summary()
    assert only.completed_runs == 2 and only.verification_applicable_runs == 1
    assert only.verification_pass_rate == 1
    hybrid_model(monkeypatch, intent='clarification', clarification='请补充主题。')
    finish(chat, chat.store.send(key, '那他们呢', 'clarification'))
    hybrid_model(monkeypatch, knowledge='研究生借阅', general='阅读建议')
    finish(chat, chat.store.send(key, '混合请求', 'mixed'))
    with chat.repository._connection() as db:
        # Old clarification has no explicit applicability flag.
        row = db.execute("SELECT run_id,final_state_json FROM runs WHERE json_extract(final_state_json,'$.chat_context.intent')='clarification'").fetchone()
        state = json.loads(row['final_state_json'])
        state.pop('verification_applicable')
        db.execute('UPDATE runs SET final_state_json=? WHERE run_id=?', (json.dumps(state), row['run_id']))
    records = chat.repository.list_runs()
    summary = chat.repository.get_stats_summary()
    assert summary.model_dump() == summarize_history(records).model_dump()
    assert summary.completed_runs == 4 and summary.verification_applicable_runs == 2
    assert summary.verified_runs == 2 and summary.verification_pass_rate == 1
    assert summary.failure_types == {}


def test_interrupted_mixed_run_recovers_knowledge_parts_from_events(chat, monkeypatch):
    from src.runtime.models import TraceEvent
    hybrid_model(monkeypatch, knowledge='研究生借阅', general='阅读建议')
    key = create(chat)['conversation_id']
    completed = finish(chat, chat.store.send(key, '混合请求', 'first'))
    pending = chat.store.send(key, '混合请求', 'second')
    for event in completed.events:
        chat.repository.append_event(TraceEvent.model_validate({**event.model_dump(), 'run_id': pending['run_id']}))
        if event.node == 'chat_general' and event.event_type == 'node_started':
            break
    chat.manager.settle(pending['run_id'], 'interrupted', 'execution_interrupted', '模拟进程中断')
    recovered = chat.repository.get_run(pending['run_id'])
    assert recovered.status == 'interrupted'
    assert recovered.final_state['verification_applicable'] is True
    assert recovered.final_state['response_parts'] == [{'kind': 'knowledge', 'content': '研究生可借 30 册。'}]
    assert chat.store.messages(key)[-1]['content'] == '研究生可借 30 册。'


def test_scope_uses_all_frozen_sources_without_agent_prompts_or_paths():
    from src.runtime.chat_context import system_scope
    snapshot = {'system_ref': {'name': '多知识源系统'}, 'agents': {
        'planner': {'role': 'planner', 'model_config': {'model_name': 'frozen', 'system_prompt': '私人提示词'}}},
        'sources': {'a': {'source_id': 'a', 'name': '借阅规则', 'version': 2, 'resolved_path': 'private-path',
                         'files': [{'filename': 'policy.md', 'sha256': 'internal-hash'}]},
                    'b': {'source_id': 'b', 'name': '技术手册', 'version': 3}}}
    scope = system_scope(snapshot)
    assert {s['source_id'] for s in scope['sources']} == {'a', 'b'}
    assert scope['sources'][0]['files'] == [{'filename': 'policy.md'}]
    assert '私人提示词' not in json.dumps(scope, ensure_ascii=False)
    assert 'private-path' not in json.dumps(scope)
