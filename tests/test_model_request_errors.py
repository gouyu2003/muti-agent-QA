"""Offline regression for provider errors, bounded retries and accounting."""
import json
import socket
from types import SimpleNamespace

import httpx
from openai import APIStatusError, APIConnectionError, APITimeoutError
import pytest

from src.agents.structured_output import invoke_json
from src.llm_usage import LLMUsageCollector
from src.model_errors import ModelRequestError, error_diagnostic, request_errors


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Real network calls are forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def provider_error(status=400, code='data_inspection_failed', message='Input text data may contain inappropriate content.'):
    response = httpx.Response(status, json={'request_id': 'response-id'},
        headers={'x-request-id': 'header-id'}, request=httpx.Request('POST', 'https://provider.invalid/v1'))
    return APIStatusError('SDK raw exception must not be stored', response=response,
                          body={'error': {'code': code, 'message': message}})


class Chat:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def message(text='{"answer":"A"}'):
    return SimpleNamespace(content=text, response_metadata={'finish_reason': 'stop'},
        usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})


@pytest.mark.parametrize('status,code,category,retryable', [
    (400, 'data_inspection_failed', 'content_filter', False),
    (400, 'DataInspectionFailed', 'content_filter', False),
    (400, 'InvalidParameter', 'bad_request_details_required', False),
    (401, 'InvalidApiKey', 'authentication_or_permission', False),
    (403, 'AccessDenied', 'authentication_or_permission', False),
    (429, 'AllocationQuota.FreeTierOnly', 'billing_or_quota', False),
    (429, 'insufficient_quota', 'billing_or_quota', False),
    (429, 'Throttling', 'rate_limit_or_quota', True),
    (408, 'RequestTimeout', 'timeout', True),
    (503, 'ServiceUnavailable', 'provider_service_error', True),
    (501, 'NotImplemented', 'provider_service_error', False),
])
def test_error_categories_do_not_blindly_retry_400_or_billing(status, code, category, retryable):
    result = error_diagnostic(provider_error(status, code, 'Provider error'))
    assert (result['category'], result['retryable']) == (category, retryable)
    assert result['request_id'] == 'header-id'


@pytest.mark.parametrize('exc_type,category', [(APIConnectionError, 'connection_error'), (APITimeoutError, 'timeout')])
def test_transport_errors_are_explicit_retry_candidates(exc_type, category):
    exc = exc_type(request=httpx.Request('POST', 'https://provider.invalid/v1'))
    result = error_diagnostic(exc)
    assert result['category'] == category and result['retryable']


def test_content_filter_stops_without_format_repairs_and_redacts_diagnostics(monkeypatch):
    prompt = 'PRIVATE_PROMPT_LINE_MUST_NOT_BE_STORED\nSECOND_PRIVATE_PROMPT_LINE'
    key = 'test-secret-without-sk-prefix'
    monkeypatch.setenv('DEEPSEEK_API_KEY', '  ' + key + '\n')
    error = provider_error(message=f'Input text data may contain inappropriate content. {prompt} {key} '
                                  'Bearer private-token https://private.example/v1')
    usage = LLMUsageCollector().for_agent('reviewer')
    chat = Chat(error, message())
    with pytest.raises(ModelRequestError) as raised:
        invoke_json(chat, prompt, 2, usage, validate=lambda _data: None)
    assert raised.value.error_type == 'provider_content_filter'
    assert len(chat.prompts) == usage.snapshot().call_count == 1
    assert usage.snapshot().total_tokens is None
    attempt = usage.attempts()[0]
    assert attempt['outcome'] == 'request_error' and not attempt['will_retry']
    saved = json.dumps(attempt)
    for private in (key, 'PRIVATE_PROMPT_LINE', 'SECOND_PRIVATE', 'private-token', 'private.example', 'SDK raw'):
        assert private not in saved


def test_transient_retry_is_visible_reuses_prompt_and_rechecks_budget(monkeypatch):
    usage = LLMUsageCollector()
    reserves, waits = [], []
    usage.before_invoke = reserves.append
    monkeypatch.setattr(usage.cancelled, 'wait', lambda delay: waits.append(delay))
    chat = Chat(provider_error(503, 'ServiceUnavailable', 'Temporarily unavailable'), message())
    result, diagnostics = invoke_json(chat, 'JSON', 2, usage, validate=lambda _data: None)
    assert result == {'answer': 'A'}
    assert chat.prompts == reserves == ['JSON', 'JSON']
    assert waits == [1.0]
    assert usage.snapshot().call_count == 2 and usage.snapshot().total_tokens is None
    assert [c['outcome'] for c in diagnostics['calls']] == ['request_error', 'ok']
    assert diagnostics['retry_count'] == 1
    assert diagnostics['calls'][0]['will_retry']


def test_transient_retry_is_bounded_independently_of_format_retry_count(monkeypatch):
    usage = LLMUsageCollector()
    monkeypatch.setattr(usage.cancelled, 'wait', lambda _delay: False)
    chat = Chat(*[provider_error(429, 'Throttling', 'Too many requests') for _ in range(5)])
    with pytest.raises(ModelRequestError):
        invoke_json(chat, 'JSON', 2, usage, validate=lambda _data: None)
    assert len(chat.prompts) == usage.snapshot().call_count == 2
    assert [c['will_retry'] for c in usage.attempts()] == [True, False]


def test_request_retry_and_output_repair_keep_all_attempts(monkeypatch):
    usage = LLMUsageCollector()
    monkeypatch.setattr(usage.cancelled, 'wait', lambda _delay: False)
    chat = Chat(provider_error(500, 'InternalError', 'Service failure'), message('invalid json'), message())
    _, diagnostics = invoke_json(chat, 'JSON', 1, usage, validate=lambda _data: None)
    assert [c['outcome'] for c in diagnostics['calls']] == ['request_error', 'invalid_json', 'ok']
    assert diagnostics['retry_count'] == 2 and usage.snapshot().call_count == 3
    assert chat.prompts[:2] == ['JSON', 'JSON'] and '上一响应无效' in chat.prompts[2]


def test_cancellation_during_backoff_stops_before_second_call(monkeypatch):
    usage = LLMUsageCollector()
    def cancel(_delay):
        usage.cancelled.set()
    monkeypatch.setattr(usage.cancelled, 'wait', cancel)
    chat = Chat(provider_error(503, 'ServiceUnavailable', 'Service failure'), message())
    with pytest.raises(RuntimeError, match='取消'):
        usage.invoke(chat, 'JSON')
    assert len(chat.prompts) == usage.snapshot().call_count == 1


def test_local_budget_rejection_is_not_wrapped_or_retried(monkeypatch):
    usage = LLMUsageCollector()
    monkeypatch.setattr(usage.cancelled, 'wait', lambda _delay: False)
    reserves = []
    def reserve(_prompt):
        reserves.append(1)
        if len(reserves) > 1:
            raise RuntimeError('local call budget exhausted')
    usage.before_invoke = reserve
    chat = Chat(provider_error(503, 'ServiceUnavailable', 'Service failure'), message())
    with pytest.raises(RuntimeError, match='local call budget'):
        usage.invoke(chat, 'JSON')
    assert len(chat.prompts) == usage.snapshot().call_count == 1
    assert len(usage.attempts()) == 1


def test_historical_unknown_request_errors_are_not_reclassified():
    assert request_errors({'agent': {'calls': [{'outcome': 'request_error'}]}}) == []
