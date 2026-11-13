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
        assert config['agent_id'] == 'agent' and config['model_config']['max_tokens'] == 512
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
