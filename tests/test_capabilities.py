"""Fault-oriented acceptance of background QA, observations and Chinese retrieval."""
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from types import SimpleNamespace
from langchain_core.messages import AIMessage
from src.llm_usage import LLMUsageCollector
from src.runtime.protocol_agents import debate_message
from src.runtime.protocol_runtime import ProtocolExecutionError
from threading import Event, Thread

import pytest

from backend.app import build_api_run_context
from src.agents.retriever import build_evidence_groups
from src.knowledge.store import KnowledgeStore, retriever_from_corpus_snapshot
from src.observability import RunObserver, current_observer, summarize_timings, read_observations
from src.runtime.interactive import ExecutionFailure, InteractiveRuns
from src.runtime.interactive_worker import execute
from src.runtime.models import RunRecord, TraceEvent
from src.runtime.repository import RepositoryError, InvalidRunTransitionError
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository, _CREATE_V1_SCHEMA_SQL


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteRunRepository(tmp_path / 'runs.sqlite3')
    repo.initialize()
    return repo


class Graph:
    def __init__(self, usage, ready=None):
        self.usage, self.ready = usage, ready

    def stream(self, *args, **kwargs):
        yield {'type': 'tasks', 'data': {'name': 'planner_v0', 'input': {}}}
        yield {'type': 'updates', 'data': {'planner_v0': {'sub_questions': [
            {'id': 'sq_1', 'question': '保存的子问题', 'keywords': [], 'depends_on': []}]}}}
        if self.ready:
            self.ready.set()
            self.usage.cancelled.wait(10)
        yield {'type': 'tasks', 'data': {'name': 'answer_validator_v0', 'input': {}}}
        yield {'type': 'updates', 'data': {'answer_validator_v0': {'answer': '隔离答案', 'verified': True, 'failure_type': 'none'}}}


def context(run_id='background-1'):
    return build_api_run_context('Q', run_id=run_id, workflow_id='multi_hop_qa_fixed')


def test_idempotent_submission_reserves_one_run_and_one_worker(repository):
    launches = []
    manager = InteractiveRuns(repository, launcher=lambda _m, c: launches.append(c.run_id))
    first = manager.submit(context(), 'client-1', 'body-1')
    assert first['status'] == 'queued'
    assert manager.submit(context('unused'), 'client-1', 'body-1')['run_id'] == first['run_id']
    assert launches == ['background-1'] and repository.count_runs() == 1
    with pytest.raises(ExecutionFailure):
        manager.submit(context('conflict'), 'client-1', 'changed-body')
    with pytest.raises(ExecutionFailure):
        manager.submit(context('second'), 'client-2', 'body-2')


def test_execution_survives_no_subscriber_and_replay_does_not_repeat_work(repository):
    manager = InteractiveRuns(repository, launcher=lambda *_: None)
    manager.submit(context(), 'client-1', 'body')
    execute(repository, 'background-1', manager.row('background-1')['token'],
            service=RunService(repository, graph_factory=lambda usage: Graph(usage)))
    assert manager.status('background-1')['status'] == 'completed'
    events = repository.get_events('background-1')
    resumed = ''.join(manager.subscribe('background-1', after=events[-2].sequence))
    assert 'event: run_completed' in resumed and 'event: node_started' not in resumed
    assert repository.count_runs() == 1
    assert repository.get_run_detail('background-1').run.final_state['answer'] == '隔离答案'
    assert manager.cancel('background-1')['status'] == 'completed'


def test_cancel_is_confirmed_and_preserves_completed_nodes(repository):
    manager = InteractiveRuns(repository, launcher=lambda *_: None)
    manager.submit(context(), 'client-1', 'body')
    ready = Event()
    errors = []
    def work():
        try:
            execute(repository, 'background-1', manager.row('background-1')['token'],
                    service=RunService(repository, graph_factory=lambda usage: Graph(usage, ready)))
        except Exception as exc:
            errors.append(exc)
    worker = Thread(target=work)
    worker.start()
    assert ready.wait(5)
    assert manager.cancel('background-1')['status'] == 'cancelling'
    worker.join(8)
    assert not worker.is_alive() and not errors
    detail = repository.get_run_detail('background-1')
    assert detail.run.status == 'cancelled'
    assert detail.run.final_state['sub_questions'][0]['question'] == '保存的子问题'
    assert sum(e.event_type in ('run_completed', 'run_failed', 'run_cancelled') for e in detail.events) == 1


@pytest.mark.parametrize('already_settled', [False, True])
def test_worker_only_ignores_terminal_write_race_when_terminal_is_durable(repository, already_settled):
    manager = InteractiveRuns(repository, launcher=lambda *_: None)
    manager.submit(context(), 'client-1', 'body')
    class LateResult:
        def run(self, context, **kwargs):
            if already_settled:
                manager.settle(context.run_id, 'cancelled', 'cancelled', '已停止')
            raise InvalidRunTransitionError('late terminal write')
    def work():
        execute(repository, 'background-1', manager.row('background-1')['token'], service=LateResult())
    if already_settled:
        work()
        assert repository.get_run('background-1').status == 'cancelled'
        assert len(repository.get_events('background-1')) == 1
    else:
        with pytest.raises(InvalidRunTransitionError):
            work()


def test_orphan_is_interrupted_without_replaying_or_losing_saved_nodes(repository):
    manager = InteractiveRuns(repository, launcher=lambda *_: None)
    manager.submit(context(), 'client-1', 'body')
    repository.append_event(TraceEvent(run_id='background-1', sequence=0, timestamp=datetime.now(timezone.utc),
        event_type='node_completed', payload={'output': {'sub_questions': [{'id': 'saved'}]}}))
    with repository._connection() as db:
        db.execute("UPDATE interactive_runs SET phase='running'")
    assert manager.status('background-1')['status'] == 'interrupted'
    assert repository.get_run('background-1').final_state['sub_questions'] == [{'id': 'saved'}]
    assert repository.get_stats_summary().interrupted_runs == 1


def test_terminal_event_rolls_back_if_state_update_fails(repository):
    run = RunRecord(context=context(), status='running', started_at=datetime.now(timezone.utc))
    repository.create_run(run)
    event = TraceEvent(run_id=run.context.run_id, sequence=0, event_type='run_completed', timestamp=run.started_at)
    wrong = run.model_copy(update={'context': context('different'), 'status': 'completed', 'finished_at': run.started_at})
    with pytest.raises(RepositoryError):
        repository.finalize_run(wrong, event)
    assert repository.get_events(run.context.run_id) == []
    assert repository.get_run(run.context.run_id).status == 'running'


def test_execution_failure_is_not_misclassified_as_user_cancel(repository):
    class BrokenGraph:
        def stream(self, *args, **kwargs):
            yield {'type': 'tasks', 'data': {'name': 'planner_v0', 'input': {}}}
            raise RuntimeError('local execution failure')
    service = RunService(repository, graph_factory=lambda usage: BrokenGraph())
    result = service.run(context(), cancellation=Event())
    assert result.run.status == 'failed'
    assert result.events[-1].event_type == 'run_failed'


def test_v1_database_upgrade_is_backed_up_and_preserves_trace(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript(_CREATE_V1_SCHEMA_SQL.replace(", 'interrupted'", '') +
                         'CREATE TABLE schema_meta(version INTEGER PRIMARY KEY,applied_at TEXT);')
        db.execute("INSERT INTO schema_meta VALUES (1,'old')")
    repository = SQLiteRunRepository(path)
    repository.initialize()
    assert path.with_suffix('.sqlite3.v1-backup').is_file()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT MAX(version) FROM schema_meta').fetchone()[0] == 2
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_parallel_wall_time_is_interval_union_not_sum():
    records = [dict(kind='activity_finished', name='model_request', offset_ms=0, duration_ms=12000),
               dict(kind='activity_finished', name='model_request', offset_ms=1000, duration_ms=10000)]
    data = summarize_timings(records)['categories']['model_request']
    assert data == {'wall_ms': 12000, 'cumulative_ms': 22000, 'count': 2}


def test_observer_isolated_from_consumer_context(repository):
    service = RunService(repository, graph_factory=lambda usage: Graph(usage))
    stream = service.stream(context())
    next(stream)
    assert current_observer.get() is None
    closer = Thread(target=stream.close)
    closer.start(); closer.join(5)
    assert repository.get_run('background-1').status == 'cancelled'


def test_mixed_index_and_cross_hop_entities_preserve_literal_locations(tmp_path):
    store = KnowledgeStore(tmp_path / 'knowledge')
    source = store.import_files('local', '中文材料', [
        ('人物.md', '# 人物\n\n张宁创办了“清禾研究院”。'.encode()),
        ('机构.md', '# 清禾研究院\n\n清禾研究院位于苏州市。'.encode()),
        ('型号.txt', '设备 USB-C 接口支持 PD 充电，报修编号为 A120。'.encode())])
    snapshot = store.resolve('local', source['source_id'], 1)
    assert snapshot['index_version'] == 'bm25-zh-mixed-v1'
    retriever = retriever_from_corpus_snapshot(snapshot)
    hits = retriever.search('USB-C PD 充电', top_k=1)
    assert hits[0]['filename'] == '型号.txt'
    groups = build_evidence_groups([
        dict(id='sq_1', question='张宁创办了什么？', keywords=[], depends_on=[]),
        dict(id='sq_2', question='该机构位于哪里？', keywords=[], depends_on=['sq_1'])], retriever, top_k=1)
    assert '清禾研究院' in groups[0]['chunks'][0]['text']
    assert '苏州市' in groups[1]['chunks'][0]['text']
    assert groups[1]['chunks'][0]['source_version'] == 1
    assert groups[1]['chunks'][0]['line_start'] == 3
    assert retriever.search('qxzvv unknownword') == []


def test_fixed_thirty_question_retrieval_acceptance(tmp_path):
    data = json.loads((Path(__file__).parent / 'fixtures/chinese_retrieval.json').read_text(encoding='utf-8'))
    assert len(data['queries']) == 30
    store = KnowledgeStore(tmp_path / 'knowledge')
    source = store.import_files('local', '固定检索验收集', [(name, content.encode()) for name, content in data['documents'].items()])
    retriever = retriever_from_corpus_snapshot(store.resolve('local', source['source_id'], 1))
    hits = 0
    for query in data['queries']:
        if query.get('empty'):
            assert not retriever.search(query['query'], top_k=5)
        elif query.get('no_answer'):
            # Related snippets can be retrieved without containing the requested fact.
            results = retriever.search(query['query'], top_k=5)
            assert any(item['filename'] == query['file'] for item in results)
            assert query['missing_fact'] not in data['documents'][query['file']]
        elif query.get('first'):
            groups = build_evidence_groups([
                dict(id='sq_1', question=query['first'], keywords=[], depends_on=[]),
                dict(id='sq_2', question='该机构位于哪里？', keywords=[], depends_on=['sq_1'])], retriever, top_k=1)
            assert query['entity'] in groups[0]['chunks'][0]['text']
            assert query['answer'] in groups[1]['chunks'][0]['text']
            hits += 1
        else:
            hits += any(item['filename'] == query['file'] for item in retriever.search(query['query'], top_k=5))
    assert hits / 25 >= .9


@pytest.mark.parametrize('mode', ['complete', 'cancel', 'timeout'])
def test_real_child_process_uses_fake_graph_and_bounded_shutdown(repository, mode):
    """A real OS process, with no provider/model construction or network access."""
    manager = InteractiveRuns(repository, launcher=lambda *_: None)
    ctx = context()
    if mode == 'timeout':
        ctx.config_snapshot['protocol_document'] = {'spec': {'limits': {'timeout_seconds': 1, 'node_timeout_seconds': 120}}}
    manager.submit(ctx, 'client-1', 'body')
    code = '''
import sys
from src.interactive_bootstrap import bootstrap
def load_runtime():
 from threading import Event
 from src.runtime.interactive_worker import execute
 from src.runtime.service import RunService
 from src.runtime.sqlite_repository import SQLiteRunRepository
 class FakeGraph:
  def stream(self,*args,**kwargs):
   yield {'type':'tasks','data':{'name':'planner_v0','input':{}}}
   yield {'type':'updates','data':{'planner_v0':{'sub_questions':[{'id':'saved'}]}}}
   if sys.argv[3] != 'complete': Event().wait(60)
   yield {'type':'updates','data':{'answer_validator_v0':{'answer':'fake-child','verified':True}}}
 def run(database,run_id,token,*,lock_owned):
  repo=SQLiteRunRepository(database)
  execute(repo,run_id,token,service=RunService(repo,graph_factory=lambda usage:FakeGraph()),lock_owned=lock_owned)
 return run
bootstrap(sys.argv[1],'background-1',sys.argv[2],loader=load_runtime)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(repository.db_path), manager.row('background-1')['token'], mode],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # Cold Windows imports are outside the execution deadline. Runtime shutdown remains bounded below.
        deadline = monotonic()+60
        while not any(e.event_type == 'node_completed' for e in repository.get_events('background-1')):
            assert process.poll() is None
            assert monotonic() < deadline
            sleep(.05)
        if mode == 'cancel':
            manager.cancel('background-1')
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr.decode(errors='replace')
        saved = repository.get_run('background-1')
        assert saved.status == {'complete': 'completed', 'cancel': 'cancelled', 'timeout': 'failed'}[mode]
        assert saved.final_state['sub_questions'] == [{'id': 'saved'}]
        if mode == 'timeout':
            assert saved.error.error_type == 'timeout'
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.parametrize('case,code,path', [
    ('target', 'critique_targets_mismatch', 'critiques.target_call_id'),
    ('evidence', 'critique_unknown_evidence', 'critiques.evidence_ids'),
    ('response', 'revision_responses_mismatch', 'responses.critique_call_id'),
    ('missing', 'revision_missing_citation', 'supporting_evidence'),
    ('citation', 'revision_unknown_citation', 'supporting_evidence'),
])
def test_five_debate_business_rules_are_persistable_and_fed_back(monkeypatch, case, code, path):
    critique = case in ('target', 'evidence')
    reads = [dict(kind='propose', call_id='own', agent_id='a', round=1, output={}),
             dict(kind='propose', call_id='peer', agent_id='b', round=1, output={}),
             dict(kind='critique', call_id='incoming', agent_id='b', round=1,
                  output={'critiques': [{'target_call_id': 'own'}]})]
    if critique:
        body = {'summary': 'check', 'critiques': [{'target_call_id': 'wrong' if case == 'target' else 'peer',
                'stance': 'challenge', 'issue': 'check', 'evidence_ids': ['unknown'] if case == 'evidence' else ['e1']}]}
    else:
        body = dict(answer='answer', verified=True, failure_type='none', verification_note='check', change_note='check',
                    supporting_evidence=[] if case == 'missing' else [{'sq_id': 'sq_1', 'evidence_id': 'unknown' if case == 'citation' else 'e1'}],
                    responses=[] if case == 'response' else [dict(critique_call_id='incoming', accepted=True, response='check')])
    prompts = []
    class FakeChat:
        def invoke(self, prompt):
            prompts.append(prompt)
            return AIMessage(content=json.dumps(body))
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda *_: FakeChat())
    usage = LLMUsageCollector()
    with pytest.raises(ProtocolExecutionError):
        debate_message(SimpleNamespace(kind='critique' if critique else 'revise', instructions=''), 'a',
            {'model_config': {'system_prompt': '', 'max_tokens': 128}, 'failure_handling': {'max_retries': 1}},
            {'question': 'Q', 'reads': reads, 'peer_ids': ['b']},
            {'evidence_chunks': [{'sq_id': 'sq_1', 'chunks': [{'evidence_id': 'e1'}]}]}, usage)
    assert len(prompts) == 2 and code in prompts[-1]
    assert usage.attempts()[-1]['validation_errors'][0]['code'] == code
    assert usage.attempts()[-1]['validation_errors'][0]['path'] == path


def test_bootstrap_owns_queued_run_during_slow_import_and_can_cancel(repository):
    manager = InteractiveRuns(repository, launcher=lambda *_: None)
    manager.submit(context(), 'client-1', 'body')
    code = '''
import sys,time
from src.interactive_bootstrap import bootstrap
def slow_import():
 time.sleep(90)
 raise AssertionError('Cancelled startup must never reach the runtime')
bootstrap(sys.argv[1],'background-1',sys.argv[2],loader=slow_import)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(repository.db_path), manager.row('background-1')['token']],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = monotonic()+12
        while manager.row('background-1')['heartbeat_at'] is None:
            assert process.poll() is None and monotonic() < deadline
            sleep(.05)
        with repository._connection() as db:
            db.execute("UPDATE interactive_runs SET created_at='2000-01-01T00:00:00+00:00'")
        assert manager.status('background-1')['status'] == 'queued'
        manager.cancel('background-1')
        _, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stderr.decode(errors='replace')
        assert manager.status('background-1')['status'] == 'cancelled'
        assert repository.get_run('background-1').metrics.llm_call_count is None or repository.get_run('background-1').metrics.llm_call_count == 0
    finally:
        if process.poll() is None:
            process.kill(); process.communicate(timeout=5)
