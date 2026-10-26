"""Offline checks for stored-input reconstruction, call bounds and redaction."""
from copy import deepcopy
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from uuid import uuid4

import httpx
from openai import BadRequestError
import pytest

from scripts import diagnose_smoke_requests as diagnostic
from src.evaluation.experiment_config import ExperimentConfig
from src.evaluation.experiment_methods import method_definition, build_context


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Real network/model calls are forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(diagnostic, 'build_chat_model', forbidden)
    monkeypatch.setattr('src.llm.build_chat_model', forbidden)


def failed_detail(tmp_path, method):
    config = ExperimentConfig(dataset=str(tmp_path/'dataset.json'), split_directory=str(tmp_path/'splits'),
                              output=str(tmp_path/'smoke'), purpose='smoke')
    record = dict(_id='q1', question='Where is Cedar Institute?', context=[
        ['Cedar Institute', ['Cedar Institute is in Northbridge.', 'It was founded in 2008.']],
        ['Other', ['This is unrelated context.']]])
    definition = method_definition(config, method)
    context = build_context(definition, record, Path(config.output), 'smoke', str(uuid4()), 'test')
    critique = method == 'debate'
    agent_id, stage = ('experts_0', 'critique') if critique else ('reviewer_0', 'review')
    reads = [dict(call_id=f'opinion_{i}', agent_id=f'experts_{i}', stage_id='initial', round=1,
                  invocation_index=i+1, kind='propose', output=dict(answer='Northbridge', evidence_chunks=[]))
             for i in range(2)] if critique else []
    inputs = dict(question=record['question'], reads=reads, retry_counters={},
                  task=dict(agent_id=agent_id, task_id='diagnostic_task', instruction=''))
    if critique:
        inputs['peer_ids'] = ['experts_1']
    call = dict(call_id='failed_node', agent_id=agent_id, stage_id=stage, kind=stage, round=1,
                invocation_index=3, status='failed', input=inputs, output={'error':'protocol task failed'})
    return dict(run=dict(context=context.model_dump(mode='json'), status='failed',
                         final_state=dict(protocol_calls={'failed_node':call}),
                         metrics=dict(agent_usage={agent_id:dict(calls=[{'outcome':'request_error'}])})))


@pytest.mark.parametrize('method', ['pipeline_adaptive', 'debate'])
def test_reconstructs_original_phase_offline_without_mutating_inputs(tmp_path, method):
    from src.agents import answer_validator
    original_builder = answer_validator.default_llm
    detail = failed_detail(tmp_path, method)
    before = deepcopy(detail)
    first = diagnostic.reconstruct(detail)
    second = diagnostic.reconstruct(detail)
    assert first.prompt == second.prompt
    assert first.metadata['prompt_sha256'] == second.metadata['prompt_sha256']
    assert 'Where is Cedar Institute?' in first.prompt
    if method == 'debate':
        assert 'target_call_ids' in first.prompt and 'opinion_1' in first.prompt
    assert detail == before
    assert answer_validator.default_llm is original_builder


def test_snapshot_change_is_rejected(tmp_path):
    detail = failed_detail(tmp_path, 'debate')
    detail['run']['context']['config_snapshot']['execution_mode'] = 'parallel'
    with pytest.raises(ValueError, match='frozen'):
        diagnostic.reconstruct(detail)


def test_run_metadata_is_checked_separately(tmp_path):
    detail = failed_detail(tmp_path, 'debate')
    detail['run']['context']['config_snapshot']['_run_metadata']['sample_id'] = 'another-question'
    with pytest.raises(ValueError, match='frozen'):
        diagnostic.reconstruct(detail)


@pytest.mark.parametrize('nested', [False, True])
def test_preserves_provider_error_and_request_id_without_secrets(nested):
    error = dict(code='data_inspection_failed', message='Input blocked sk-test-secret https://private.example/v1')
    body = {'error':error} if nested else error
    response = httpx.Response(400, headers={'x-request-id':'new-request-id'}, json=body,
                              request=httpx.Request('POST', 'https://private.example/v1'))
    exc = BadRequestError('Bad request', response=response, body=body)
    result = diagnostic.error_diagnostic(exc, ('sk-test-secret', 'https://private.example/v1'))
    assert result['http_status'] == 400
    assert result['provider_code'] == 'data_inspection_failed'
    assert result['request_id'] == 'new-request-id'
    assert result['category'] == 'content_filter'
    assert 'sk-test-secret' not in json.dumps(result)
    assert 'private.example' not in json.dumps(result)


def test_one_probe_sends_once_and_does_not_retry_or_store_prompt(monkeypatch):
    class Chat:
        max_retries = 0
        calls = 0
        def invoke(self, prompt, **_kwargs):
            self.calls += 1
            response = httpx.Response(400, request=httpx.Request('POST', 'https://provider.invalid/v1'))
            raise BadRequestError('blocked', response=response,
                                  body={'code':'InvalidParameter','message':'invalid request: ' + prompt})
    chat = Chat()
    monkeypatch.setattr(diagnostic, 'build_chat_model', lambda _config: chat)
    probe = diagnostic.Probe({'original_run_id':'original'}, {}, 'PRIVATE_PROMPT_MUST_NOT_BE_SAVED')
    result = diagnostic.send_once(probe)
    assert chat.calls == result['model_calls'] == 1
    assert result['error']['provider_code'] == 'InvalidParameter'
    assert 'PRIVATE_PROMPT_MUST_NOT_BE_SAVED' not in json.dumps(result)


def test_request_id_is_recovered_from_original_response_body():
    error = {'code':'data_inspection_failed','message':'Input blocked'}
    response = httpx.Response(400, json={'error':error,'request_id':'body-request-id'},
                              request=httpx.Request('POST', 'https://provider.invalid/v1'))
    # The SDK may expose only the nested error as exc.body.
    exc = BadRequestError('blocked', response=response, body=error)
    assert diagnostic.error_diagnostic(exc)['request_id'] == 'body-request-id'


def test_success_reports_usage_but_never_response_content(monkeypatch):
    response = SimpleNamespace(content='PRIVATE_ANSWER', usage_metadata={
        'input_tokens':100, 'output_tokens':20, 'total_tokens':120},
        response_metadata={'headers':{'x-request-id':'success-request'},'finish_reason':'stop'})
    chat = SimpleNamespace(max_retries=0, invoke=lambda *_a, **_k: response)
    monkeypatch.setattr(diagnostic, 'build_chat_model', lambda _config: chat)
    result = diagnostic.send_once(diagnostic.Probe({}, {}, 'private prompt'))
    assert result['outcome'] == 'request_succeeded'
    assert result['usage']['total_tokens'] == 120
    assert result['request_id'] == 'success-request'
    assert 'PRIVATE_ANSWER' not in json.dumps(result)


def test_sdk_retries_must_remain_disabled(monkeypatch):
    chat = SimpleNamespace(max_retries=2)
    monkeypatch.setattr(diagnostic, 'build_chat_model', lambda _config: chat)
    result = diagnostic.send_once(diagnostic.Probe({}, {}, 'prompt'))
    assert result['outcome'] == 'request_error' and result['model_calls'] == 0


def test_default_preview_never_reads_credentials_or_writes_diagnostics(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(diagnostic, 'ROOT', tmp_path)
    monkeypatch.setattr(diagnostic, 'prepare', lambda *_a: (
        [diagnostic.Probe({'method':'debate'}, {}, 'private prompt')], {'identity':{'source_sha256':'source'}}))
    def forbidden():
        raise AssertionError('Offline preview must not read provider credentials')
    monkeypatch.setattr(diagnostic, 'provider_identity', forbidden)
    assert diagnostic.main([]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['model_calls'] == 0 and preview['maximum_model_calls'] == 1
    assert not (tmp_path/'data').exists()


def test_more_than_two_failures_refused_before_any_request(monkeypatch, tmp_path):
    provenance = {'execution':'real_model','identity':{}}
    monkeypatch.setattr(diagnostic, 'load_frozen', lambda _path: (
        SimpleNamespace(purpose='smoke'), [], {}, provenance))
    monkeypatch.setattr(diagnostic, 'identity', lambda: {})
    rows = [dict(run_id=str(uuid4()), status='failed', attempt=1) for _ in range(3)]
    (tmp_path/'results.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError, match='maximum two'):
        diagnostic.prepare(tmp_path)


def test_changed_runtime_identity_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostic, 'load_frozen', lambda _path: (
        SimpleNamespace(purpose='smoke'), [], {}, {'execution':'real_model','identity':{'source':'old'}}))
    monkeypatch.setattr(diagnostic, 'identity', lambda: {'source':'changed'})
    with pytest.raises(ValueError, match='changed'):
        diagnostic.prepare(tmp_path)


def test_live_opt_in_writes_separate_results_and_keeps_original(monkeypatch, tmp_path, capsys):
    original = tmp_path/'data/experiments/original'
    original.mkdir(parents=True)
    artifact = original/'runs.sqlite3'
    artifact.write_bytes(b'ORIGINAL_EXPERIMENT')
    probes = [diagnostic.Probe({'original_run_id':str(i)}, {}, 'prompt') for i in range(2)]
    provenance = {'identity':{'source_sha256':'source'}, 'provider_sha256':'endpoint'}
    monkeypatch.setattr(diagnostic, 'ROOT', tmp_path)
    monkeypatch.setattr(diagnostic, 'prepare', lambda *_a: (probes, provenance))
    monkeypatch.setattr(diagnostic, 'provider_identity', lambda: 'endpoint')
    sends = []
    def send(probe, _secrets):
        sends.append(probe)
        return {**probe.metadata,'outcome':'request_error','model_calls':1,
                'error':{'provider_code':'data_inspection_failed'}}
    monkeypatch.setattr(diagnostic, 'send_once', send)
    assert diagnostic.main(['--experiment',str(original),'--allow-model-calls']) == 1
    assert len(sends) == 2 and artifact.read_bytes() == b'ORIGINAL_EXPERIMENT'
    assert list(original.iterdir()) == [artifact]
    output = list((tmp_path/'data/experiments/diagnostics').glob('*.jsonl'))
    assert len(output) == 1
    assert len(output[0].read_text().splitlines()) == 2
    assert 'private prompt' not in capsys.readouterr().out


def test_endpoint_change_refused_before_probe_or_output(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostic, 'ROOT', tmp_path)
    monkeypatch.setattr(diagnostic, 'prepare', lambda *_a: (
        [diagnostic.Probe({}, {}, 'prompt')],
        {'identity':{'source_sha256':'source'},'provider_sha256':'old-endpoint'}))
    monkeypatch.setattr(diagnostic, 'provider_identity', lambda: 'different-endpoint')
    assert diagnostic.main(['--allow-model-calls']) == 2
    assert not (tmp_path/'data').exists()
