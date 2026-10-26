"""No network or API credentials: exercise real graphs and crash recovery locally."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from src.evaluation.experiment_config import ExperimentConfig, Splits, digest, atomic_json, read_json, load_config
from src.evaluation.experiment_data import prepare_data, selected_data, sample, validate_dataset
from src.evaluation.experiment_methods import method_definition, build_context
from src.evaluation.experiment_store import Ledger, experiment_lock
from src.evaluation.experiment_scores import joint_scores, score_detail
from src.evaluation.experiment import execute, preflight, report, main, load_frozen
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.system import retriever_from_snapshot


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real model/network calls are forbidden in offline experiments')
    monkeypatch.setattr('src.llm.build_chat_model', forbidden)
    import socket
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


@pytest.fixture
def config(tmp_path):
    records = [dict(_id=f'q{i}', question=f'Where is Cedar Institute {i}?',
        type='bridge' if i % 2 else 'comparison', answer='Northbridge',
        supporting_facts=[[f'Cedar {i}', 0]],
        context=[[f'Cedar {i}', [f'Cedar Institute {i} is in Northbridge.', 'The institute was founded in 2008.']],
                 ['Other', ['This is unrelated context.']]]) for i in range(20)]
    atomic_json(tmp_path / 'dataset.json', records)
    return ExperimentConfig(dataset=str(tmp_path / 'dataset.json'), split_directory=str(tmp_path / 'splits'),
        output=str(tmp_path / 'experiment'), splits=Splits(development=2, evaluation=4, smoke=1), bootstrap_samples=100)


def fake_phases(monkeypatch, *, fail=False, interrupt_after=None):
    from src.runtime.protocol_runtime import ProtocolExecutionError
    from src.runtime.nodes import evidence_ids
    calls = []
    def phase(stage, agent_id, model, source, inputs, usage):
        calls.append((stage.kind, agent_id, deepcopy(inputs)))
        if interrupt_after is not None and len(calls) == interrupt_after: raise KeyboardInterrupt
        if fail: raise ProtocolExecutionError('controlled failure')
        if stage.kind == 'plan':
            return {'sub_questions': [dict(id='sq_1', question=inputs['question'], keywords=['Cedar'], depends_on=[])]}
        if stage.kind == 'lead':
            if inputs['reads']:
                return dict(action='review', tasks=[], summary='Northbridge', reason='done')
            return dict(action='dispatch', tasks=[dict(task_id='work_1', agent_id='experts_0', instruction='Locate Cedar Institute')], reason='chosen')
        if source:
            chunks = retriever_from_snapshot({'source': source}).search('Cedar Northbridge', top_k=3)
            groups = evidence_ids([dict(sq_id='sq_1', chunks=chunks, sufficiency='unknown')])
        else:
            groups = [g for c in inputs['reads'] for g in c['output'].get('evidence_chunks', [])]
        evidence = [dict(c, sq_id=g['sq_id']) for g in groups for c in g['chunks']]
        return dict(answer='Northbridge', verified=True, failure_type='none', supporting_evidence=evidence,
                    evidence_chunks=groups, **({'retrieved_evidence_chunks': groups} if source else {}))
    monkeypatch.setattr('src.runtime.protocol_runtime.execute_phase', phase)
    return calls


def test_selection_integrity_disjoint_and_legacy_unchanged(config):
    result = prepare_data(config)
    assert result == {'development': 2, 'evaluation': 4, 'smoke': 1}
    root = Path(config.split_directory)
    ids = lambda p: {x['sample_id'] for x in read_json(root / f'{p}.json')['samples']}
    assert ids('smoke') <= ids('development')
    assert not ids('development') & ids('evaluation')
    assert prepare_data(config) == result
    config.seed = 99
    with pytest.raises(ValueError, match='differs'): prepare_data(config)


def test_dataset_validation_and_proportional_order_independence(config):
    rows = validate_dataset(Path(config.dataset))
    assert sample(rows, 7, 42, 'dev', 'proportional') == sample(list(reversed(rows)), 7, 42, 'dev', 'proportional')
    rows[0]['supporting_facts'] = [['missing', 5]]
    atomic_json(Path(config.dataset), rows)
    with pytest.raises(ValueError, match='coordinates'): prepare_data(config)


@pytest.mark.parametrize('method', ['single_rag', 'pipeline_fixed', 'pipeline_adaptive', 'multi_expert', 'lead', 'debate'])
def test_six_real_graphs_and_label_boundary(config, monkeypatch, tmp_path, method):
    calls = fake_phases(monkeypatch)
    prepare_data(config)
    records, _ = selected_data(config)
    gold_record = deepcopy(records[0]); gold_record['answer'] = 'GOLD_MUST_NOT_ENTER_RUNTIME'
    definition = method_definition(config, method)
    before = deepcopy(definition)
    context = build_context(definition, gold_record, tmp_path, 'offline', method, 'test')
    assert definition == before
    public = read_json(Path(context.config_snapshot['source']['resolved_path']))
    assert set(public) == {'_id', 'question', 'context'}
    assert 'GOLD_MUST_NOT_ENTER_RUNTIME' not in json.dumps(context.model_dump())
    result = RunService(SQLiteRunRepository(tmp_path / f'{method}.sqlite3')).run(context)
    assert result.run.status == 'completed', result.run.error
    assert 'GOLD_MUST_NOT_ENTER_RUNTIME' not in json.dumps(result.model_dump(mode='json'))
    kinds = [c[0] for c in calls]
    if method == 'single_rag': assert kinds == ['answer']
    if method == 'multi_expert': assert kinds.count('review') == 1 and kinds.count('propose') == 2
    if method == 'lead':
        assert [c[1] for c in calls if c[0] == 'dispatch'] == ['experts_0']
    if method == 'debate':
        assert kinds == ['propose'] * 2 + ['critique'] * 2 + ['revise'] * 2 + ['review']
        assert all(not c[2]['reads'] for c in calls if c[0] == 'propose')


@pytest.mark.parametrize('status,failures,call_limit,expected_status,expected_calls', [
    (400, 1, 40, 'failed', 1),
    (503, 1, 40, 'completed', 2),
    (503, 3, 40, 'failed', 2),
    (503, 1, 1, 'failed', 1),
])
def test_provider_errors_survive_experiment_logs_history_and_reports(
        config, monkeypatch, capsys, status, failures, call_limit, expected_status, expected_calls):
    import httpx
    from openai import APIStatusError
    from types import SimpleNamespace
    from src.model_errors import REQUEST_RETRY_POLICY

    config.purpose, config.methods = 'smoke', ['single_rag']
    config.limits.max_llm_calls = call_limit
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'PRIVATE_TEST_KEY')
    monkeypatch.setitem(REQUEST_RETRY_POLICY, 'backoff_seconds', 0)
    prompts = []
    code = 'data_inspection_failed' if status == 400 else 'ServiceUnavailable'
    class Chat:
        def invoke(self, prompt, **_kwargs):
            prompts.append(prompt)
            if len(prompts) <= failures:
                response = httpx.Response(status, headers={'x-request-id': 'test-request-id'},
                    request=httpx.Request('POST', 'https://provider.invalid/v1'))
                raise APIStatusError('UNSAFE_SDK_EXCEPTION', response=response, body={'code': code,
                    'message': 'Provider rejection PRIVATE_TEST_KEY https://private.example/v1'})
            return SimpleNamespace(content=json.dumps(dict(answer='Northbridge', verified=False,
                failure_type='insufficient', supporting_evidence=[])),
                usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda _config: Chat())
    prepare_data(config)
    target = execute(config, service_factory=RunService)
    root = Path(config.output)
    row = json.loads((root / 'results.jsonl').read_text(encoding='utf-8').splitlines()[0])
    detail = read_json(root / 'runs' / f'{row["run_id"]}.json')
    assert row['status'] == expected_status
    assert row['metrics']['llm_call_count'] == len(prompts) == expected_calls
    assert row['metrics']['total_tokens'] is None
    assert 'model_request_error' in row['labels']
    errors = [c['request_error'] for usage in row['metrics']['agent_usage'].values()
              for c in usage['calls'] if c['outcome'] == 'request_error']
    assert errors[0]['http_status'] == status and errors[0]['provider_code'] == code
    assert errors[0]['request_id'] == 'test-request-id'
    progress = json.loads((root / 'logs' / 'progress.jsonl').read_text(encoding='utf-8').splitlines()[0])
    assert progress['request_errors'][0]['provider_code'] == code
    saved = json.dumps(detail) + json.dumps(progress) + capsys.readouterr().out
    assert all(private not in saved for private in ('PRIVATE_TEST_KEY', 'private.example', 'UNSAFE_SDK_EXCEPTION'))
    if status == 400:
        assert row['error']['error_type'] == 'provider_content_filter'
        assert 'provider_content_filter' in row['labels']
        assert all(value == 0 for value in row['scores'].values())
        failed = next(c for c in detail['run']['final_state']['protocol_calls'].values() if c['status'] == 'failed')
        assert failed['output']['request_error']['provider_code'] == code
        event = next(e for e in detail['events'] if e['event_type'] == 'run_failed')
        assert event['payload']['request_errors'][0]['request_id'] == 'test-request-id'
    elif expected_status == 'completed':
        assert 'request_retry_recovered' in row['labels']
        assert any(c['outcome'] == 'ok' for usage in row['metrics']['agent_usage'].values() for c in usage['calls'])
    elif call_limit == 1:
        assert row['error']['error_type'] == 'protocol_execution_error'
        assert 'resource_limit' in row['labels']
    else:
        assert row['error']['error_type'] == 'model_request_error'
    repository = SQLiteRunRepository(root / 'runs.sqlite3')
    assert repository.get_run_detail(row['run_id']).model_dump(mode='json') == detail
    provenance = read_json(root / 'provenance.json')
    assert provenance['request_policy']['transient_errors']['max_retries'] == 1
    summary = read_json(target / 'summary.json')
    assert 'model_request_error' in json.dumps(summary)


def test_joint_metric_is_not_product_of_f1():
    # Answer P=1/2,R=1; support P=1,R=1/2 => joint P=R=1/2.
    scores = joint_scores('red blue', [['A', 0]], {'answer': 'red', 'supporting_facts': [['A', 0], ['B', 0]]})
    assert scores['f1'] == pytest.approx(2/3)
    assert scores['joint_f1'] == .5
    assert scores['joint_f1'] != scores['f1'] * scores['sp_f1']
    assert joint_scores('yes indeed', [], {'answer': 'yes', 'supporting_facts': [['A', 0]]})['joint_f1'] == 0


def test_end_to_end_resume_no_duplicates_reports(config, monkeypatch):
    config.model.name = 'offline-model'
    calls = fake_phases(monkeypatch)
    prepare_data(config)
    assert preflight(config)['planned_tasks'] == 12
    target = execute(config, service_factory=RunService)
    summary = read_json(target / 'summary.json')
    assert summary['complete']
    assert summary['provenance']['execution'] == 'offline_test'
    assert summary['provenance']['request_policy']['deepseek_thinking'] == 'not_sent'
    assert len(summary['pairs']) == 15
    assert all(m['scores']['em'] == 1 for m in summary['methods'].values())
    count = len(calls)
    execute(directory=config.output, service_factory=RunService)
    assert len(calls) == count
    assert (target / 'review.csv').exists()
    assert len(list((Path(config.output) / 'runs').glob('*.json'))) == 12
    assert (target / 'paired-differences.csv').is_file()
    with pytest.raises(ValueError, match='already exists'): execute(config, service_factory=RunService)


def test_failure_first_attempt_retained_on_explicit_retry(config, monkeypatch):
    config.methods = ['single_rag']
    fake_phases(monkeypatch, fail=True)
    prepare_data(config)
    first = execute(config, service_factory=RunService)
    assert read_json(first / 'summary.json')['methods']['single_rag']['scores']['em'] == 0
    calls = fake_phases(monkeypatch)
    execute(directory=config.output, service_factory=RunService)
    assert not calls
    retry = execute(directory=config.output, retry_status=['failed'], service_factory=RunService)
    result = read_json(retry / 'summary.json')
    assert result['methods']['single_rag']['scores']['em'] == 0
    assert result['latest_attempt_supplement']['single_rag']['scores']['em'] == 1
    assert result['retry_count'] == 2


def test_crash_after_persisted_terminal_recovers_without_model(config, monkeypatch):
    config.methods = ['single_rag']
    calls = fake_phases(monkeypatch)
    prepare_data(config)
    class Crash(RunService):
        def run(self, context):
            super().run(context)
            raise KeyboardInterrupt
    first = execute(config, service_factory=Crash)
    assert not read_json(first / 'summary.json')['complete']
    assert len(calls) == 1
    second = execute(directory=config.output, service_factory=RunService)
    assert len(calls) == 2
    assert read_json(second / 'summary.json')['complete']


def test_unknown_interruption_requires_explicit_retry(config, monkeypatch):
    config.methods = ['single_rag']
    fake_phases(monkeypatch, interrupt_after=1)
    prepare_data(config)
    execute(config, service_factory=RunService)
    calls = fake_phases(monkeypatch)
    second = execute(directory=config.output, service_factory=RunService)
    assert len(calls) == 1  # Only the never-started second task.
    assert not read_json(second / 'summary.json')['complete']
    third = execute(directory=config.output, retry_status=['interrupted'], service_factory=RunService)
    assert len(calls) == 2
    assert read_json(third / 'summary.json')['latest_attempt_supplement']['single_rag']['complete']


def test_lock_exclusion_and_release_after_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with experiment_lock(tmp_path):
            with pytest.raises(ValueError, match='locked'):
                with experiment_lock(tmp_path): pass
            raise RuntimeError('crash')
    with experiment_lock(tmp_path): pass


def test_cli_gates_network_before_loading_config(tmp_path):
    assert main(['run', '--config', str(tmp_path / 'missing')]) == 2
    assert main(['resume', '--experiment', str(tmp_path / 'missing')]) == 2


def test_tampered_manifest_method_config_rejected(config, monkeypatch):
    fake_phases(monkeypatch)
    config.methods = ['single_rag']
    prepare_data(config)
    execute(config, service_factory=RunService)
    path = Path(config.output) / 'methods/single_rag.json'
    original = read_json(path)
    altered = deepcopy(original); altered['snapshot']['agents']['expert_0']['model_config']['temperature'] = .8
    atomic_json(path, altered)
    with pytest.raises(ValueError, match='methods changed'): execute(directory=config.output, service_factory=RunService)
    atomic_json(path, original)
    original_config = read_json(Path(config.output) / 'config.resolved.json')
    original_config['repeats'] = 5
    atomic_json(Path(config.output) / 'config.resolved.json', original_config)
    with pytest.raises(ValueError, match='config'): execute(directory=config.output, service_factory=RunService)


def test_relative_config_paths_and_unknown_keys(config, tmp_path):
    value = config.model_dump(); value['dataset'] = 'dataset.json'
    atomic_json(tmp_path / 'config.json', value)
    assert load_config(tmp_path / 'config.json').dataset == config.dataset
    value['unexpected'] = 'secret'
    atomic_json(tmp_path / 'config.json', value)
    assert main(['prepare', '--config', str(tmp_path / 'config.json')]) == 2


@pytest.mark.parametrize('method', ['single_rag', 'pipeline_fixed', 'pipeline_adaptive', 'multi_expert'])
def test_actual_retrieval_and_prompt_boundary(config, monkeypatch, tmp_path, method):
    import re
    from types import SimpleNamespace
    prompts = []
    monkeypatch.setattr('src.runtime.protocol_runtime.planner_fn', lambda state, **kw: {
        'sub_questions': [dict(id='sq_1', question=state['question'], keywords=['Cedar'], depends_on=[])]})
    class Chat:
        def invoke(self, prompt, **kwargs):
            prompts.append(prompt)
            refs = [json.loads(r) for r in re.findall(r'可直接复制的引用：(\{[^\n]+\})', prompt)]
            return SimpleNamespace(content=json.dumps(dict(answer='Northbridge', verified=True,
                failure_type='none', supporting_evidence=refs, verification_note='Local fixture')),
                usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
    monkeypatch.setattr('src.agents.answer_validator.default_llm', lambda config: Chat())
    prepare_data(config)
    rows, _ = selected_data(config)
    context = build_context(method_definition(config, method), rows[0], tmp_path, 'test', method, 'offline')
    detail = RunService(SQLiteRunRepository(tmp_path / 'actual.sqlite3')).run(context)
    assert detail.run.status == 'completed', detail.run.error
    scored = score_detail(detail, rows[0])
    assert scored['scores']['em'] == 1
    assert scored['retrieval']['cumulative_support_coverage'] == 1
    assert detail.run.metrics.total_tokens == 15 * len(prompts)
    assert all('retrieved_evidence_chunks' not in p for p in prompts)


def test_published_method_read_only_and_original_prompt_preserved(config, tmp_path):
    from src.evaluation.experiment_config import PublishedSystem
    from src.runtime.system_store import fingerprint
    original = method_definition(config, 'single_rag')['snapshot']
    original['instances'][0]['model_config']['system_prompt'] = 'Preserved published prompt'
    original['config_sha256'] = fingerprint(original)
    database = tmp_path / 'published.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE releases (system_id TEXT, version INTEGER, document TEXT)')
        db.execute('INSERT INTO releases VALUES (?,?,?)', ('sys_local', 1, json.dumps({'snapshot': original})))
    before = database.read_bytes()
    config.published_systems = {'single_rag': PublishedSystem(database=str(database), system_id='sys_local', version='v1')}
    definition = method_definition(config, 'single_rag')
    assert database.read_bytes() == before
    assert definition['source']['original_sha256'] == original['config_sha256']
    assert definition['snapshot']['agents']['expert_0']['model_config']['system_prompt'] == 'Preserved published prompt'
    prepare_data(config)
    rows, _ = selected_data(config)
    context = build_context(definition, rows[0], tmp_path, 'local', 'id', 'test')
    assert all(s['source_type'] == 'hotpotqa_context' for s in context.config_snapshot['sources'].values())
    assert database.read_bytes() == before


@pytest.mark.parametrize('damage', ['task', 'ledger', 'runs'])
def test_missing_or_changed_ledger_refuses_replay(config, monkeypatch, damage):
    config.methods = ['single_rag']
    calls = fake_phases(monkeypatch)
    prepare_data(config)
    execute(config, service_factory=RunService)
    directory = Path(config.output)
    count = len(calls)
    if damage == 'task':
        with sqlite3.connect(directory / 'tasks.sqlite3') as db:
            db.execute('DELETE FROM tasks WHERE task_id=1')
    else:
        (directory / ('tasks.sqlite3' if damage == 'ledger' else 'runs.sqlite3')).unlink()
    with pytest.raises(ValueError): execute(directory=directory, service_factory=RunService)
    with pytest.raises(ValueError): report(directory)
    assert len(calls) == count


def test_changed_runtime_identity_refuses_resume(config, monkeypatch):
    config.methods = ['single_rag']
    calls = fake_phases(monkeypatch)
    prepare_data(config)
    execute(config, service_factory=RunService)
    count = len(calls)
    monkeypatch.setattr('src.evaluation.experiment.identity', lambda: {'source_sha256': 'changed'})
    with pytest.raises(ValueError, match='Code, dependencies'): execute(directory=config.output, service_factory=RunService)
    assert len(calls) == count


def test_cli_prepare_preflight_and_config_defaults(config, tmp_path):
    path = tmp_path / 'config.json'
    atomic_json(path, config.model_dump())
    assert main(['prepare', '--config', str(path)]) == 0
    assert main(['preflight', '--config', str(path)]) == 0
    root = Path(__file__).resolve().parents[1]
    for name in ('baselines', 'smoke', 'evaluation'):
        value = load_config(root / 'experiments' / f'{name}.local.json')
        assert len(value.methods) == 6
        assert value.limits.max_llm_calls == 40
    partial = config.model_dump()
    partial['limits'] = {'timeout_seconds': 300}
    assert ExperimentConfig.model_validate(partial).limits.max_llm_calls == 40


def test_lock_excludes_another_process(tmp_path):
    import subprocess
    import sys
    command = [sys.executable, '-c',
        'import sys; from src.evaluation.experiment_store import experiment_lock; '
        'lock = experiment_lock(sys.argv[1]); lock.__enter__(); lock.__exit__(None,None,None)', str(tmp_path)]
    with experiment_lock(tmp_path):
        blocked = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert blocked.returncode != 0 and 'locked by another process' in blocked.stderr
    assert subprocess.run(command, capture_output=True, timeout=20).returncode == 0


def test_incomplete_and_unknown_usage_are_not_zero_scores_or_costs():
    from src.evaluation.experiment_report import summary
    row = dict(status='completed', scores=dict(em=1, f1=1, sp_em=1, sp_f1=1, joint_em=1, joint_f1=1),
               metrics={'llm_call_count': 2, 'total_tokens': None})
    partial = summary([row], 2)
    assert not partial['complete'] and partial['scores']['em'] is None
    assert partial['observed_scores']['em'] == 1
    assert partial['metrics']['total_tokens']['reported_total'] is None
    assert partial['metrics']['total_tokens']['reported'] == 0
