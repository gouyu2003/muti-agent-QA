"""Public model-request diagnostics and an explicit, bounded retry policy."""
from __future__ import annotations

import json
import re

import httpx
from openai import APIError, APIConnectionError, APITimeoutError


REQUEST_RETRY_POLICY = {
    'max_retries': 1,
    'backoff_seconds': 1.0,
    'http_statuses': [408, 429, 500, 502, 503, 504],
    'transport_errors': ['timeout', 'connection_error'],
    'never_retry': ['content_filter', 'billing_or_quota', 'authentication_or_permission', 'bad_request_details_required'],
    'sdk_retries': 0,
}


def redact(value, secrets=()):
    text = str(value)
    # Providers sometimes echo the request, including JSON-escaped text.
    protected = {str(s) for s in secrets if s}
    protected |= {s.strip() for s in protected if s.strip()}
    protected |= {json.dumps(s, ensure_ascii=False)[1:-1] for s in protected}
    protected |= {line for s in tuple(protected) for line in s.splitlines() if len(line.strip()) >= 12}
    for secret in sorted(protected, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)\bBearer\s+[^\s\"\',;]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_.\-*]+', '[REDACTED_KEY]', text)
    text = re.sub(r'https?://[^\s\"\'<>]+', '[REDACTED_URL]', text)
    return text[:3000]


def is_model_request_error(exc):
    return isinstance(exc, (APIError, httpx.TransportError)) or isinstance(getattr(exc, 'status_code', None), int)


def error_diagnostic(exc, secrets=()):
    response = getattr(exc, 'response', None)
    body = getattr(exc, 'body', None)
    response_body = {}
    if response is not None:
        try:
            response_body = response.json()
        except Exception:
            pass
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            pass
    if body is None:
        body = response_body
    fields = body if isinstance(body, dict) else {}
    error = fields.get('error', fields)
    error = error if isinstance(error, dict) else {}
    headers = getattr(response, 'headers', None) or {}
    status = getattr(exc, 'status_code', None)
    if status is None and response is not None:
        status = getattr(response, 'status_code', None)
    code = error.get('code') or error.get('type') or fields.get('code') or getattr(exc, 'code', None)
    # Do not serialize str(exc): SDK exceptions can embed the entire response.
    message = error.get('message') or fields.get('message') or '模型请求失败；供应商未提供可用的错误详情。'
    request_id = (getattr(exc, 'request_id', None) or fields.get('request_id') or error.get('request_id')
                  or (response_body.get('request_id') if isinstance(response_body, dict) else None)
                  or headers.get('x-request-id') or headers.get('x-dashscope-request-id')
                  or headers.get('x-acs-request-id'))
    combined = re.sub(r'[^a-z0-9]', '', (str(code) + ' ' + str(message)).lower())
    if any(word in combined for word in ('datainspectionfailed', 'contentfilter', 'contentpolicyviolation', 'inappropriatecontent')):
        category = 'content_filter'
    elif any(word in combined for word in ('arrearage', 'freetieronly', 'insufficientquota', 'quotaexhausted', 'quotaexceeded')):
        category = 'billing_or_quota'
    elif status == 429:
        category = 'rate_limit_or_quota'
    elif status in (401, 403):
        category = 'authentication_or_permission'
    elif isinstance(exc, (APITimeoutError, httpx.TimeoutException)) or status == 408:
        category = 'timeout'
    elif isinstance(exc, (APIConnectionError, httpx.TransportError)):
        category = 'connection_error'
    elif isinstance(status, int) and status >= 500:
        category = 'provider_service_error'
    elif status == 400:
        category = 'bad_request_details_required'
    else:
        category = 'client_or_transport_error'
    retryable = (category not in REQUEST_RETRY_POLICY['never_retry'] and
                 (status in REQUEST_RETRY_POLICY['http_statuses'] or
                  category in REQUEST_RETRY_POLICY['transport_errors']))
    return dict(exception_type=type(exc).__name__, http_status=status,
                provider_code=redact(code, secrets) if code else None,
                message=redact(message, secrets), request_id=redact(request_id, secrets) if request_id else None,
                category=category, retryable=retryable)


class ModelRequestError(RuntimeError):
    """Only sanitized details cross the runtime/public persistence boundary."""

    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        self.error_type = ('provider_content_filter' if diagnostic['category'] == 'content_filter'
                           else 'model_request_error')
        message = ('模型供应商内容审核拦截了请求。' if diagnostic['category'] == 'content_filter'
                   else '模型请求失败。')
        if diagnostic['http_status'] is not None:
            message += f" HTTP {diagnostic['http_status']}。"
        if diagnostic['provider_code']:
            message += f" 错误码：{diagnostic['provider_code']}。"
        if diagnostic['request_id']:
            message += f" Request ID：{diagnostic['request_id']}。"
        super().__init__(message)


def request_errors(agent_usage):
    """Read per-agent diagnostics without guessing missing historical errors."""
    return [{**call['request_error'], 'agent_id': agent_id}
            for agent_id, usage in agent_usage.items() for call in usage.get('calls', [])
            if call.get('outcome') == 'request_error' and isinstance(call.get('request_error'), dict)]
