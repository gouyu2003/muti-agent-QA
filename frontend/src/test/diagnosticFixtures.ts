import type { ModelCall, StoredRunDetail } from '../types'

export const blockedCall: ModelCall = {
  request_attempt: 1, duration_ms: 872, output_chars: 0, outcome: 'request_error', will_retry: false,
  request_error: { exception_type: 'OpenAIInvalidRequestError', http_status: 400,
    provider_code: 'data_inspection_failed', category: 'content_filter',
    message: 'Input text data may contain inappropriate content.', request_id: 'request-blocked-001', retryable: false },
}

export function diagnosticDetail(status: 'failed' | 'completed' = 'failed', calls: ModelCall[] = [blockedCall]): StoredRunDetail {
  return {
    run: {
      context: { run_id: 'run_1', entrypoint: 'batch', question: 'Which university is in Northbridge?', source_id: null,
        record_id: null, workflow_id: 'debate', method_id: 'debate', model_name: 'deepseek-v4.1-flash',
        protocol_id: 'debate', code_version: 'frozen-code', config_snapshot: {}, experiment_id: 'eval_1', sample_id: 'q1' },
      status, started_at: '2026-10-28T07:00:00+00:00', finished_at: '2026-10-28T07:00:01+00:00',
      final_state: status === 'completed' ? { answer: 'Cedar Institute' } : null,
      metrics: { duration_ms: 1000, node_call_count: 1, llm_call_count: calls.length,
        prompt_tokens: null, completion_tokens: null, total_tokens: null, estimated_cost_usd: null,
        plan_retry_count: 0, retrieval_retry_count: 0, validation_retry_count: 0,
        agent_usage: { experts_0: { call_count: calls.length, total_tokens: null, calls } } },
      error: status === 'failed' ? { error_type: 'provider_content_filter', message: '模型供应商内容审核拦截了请求。 HTTP 400。' } : null,
    },
    events: [{ run_id: 'run_1', sequence: 1, event_type: status === 'failed' ? 'run_failed' : 'run_completed',
      timestamp: '2026-10-28T07:00:01+00:00', node: null, cycle: null, duration_ms: 1000,
      payload: { message: status === 'failed' ? '模型供应商内容审核拦截了请求。' : '运行完成' } }],
  }
}
