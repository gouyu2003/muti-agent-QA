import { Typography } from 'antd'
import type { ModelDiagnostics as Diagnostics, ModelCall, ModelRequestDiagnostic } from './types'
import { CALL_OUTCOMES, requestCategoryLabel } from './diagnosticSummary'

export function RequestErrorDetails({ error }: { error?: ModelRequestDiagnostic }) {
  if (!error) return <p className="diagnostic-note">仅记录请求失败；HTTP 状态码、供应商错误码和 Request ID 未记录，无法确定具体原因。</p>
  return <dl className="request-error-details">
    <div><dt>错误类别</dt><dd>{requestCategoryLabel(error.category)}</dd></div>
    <div><dt>HTTP 状态码</dt><dd>{error.http_status ?? '未记录'}</dd></div>
    <div><dt>供应商错误码</dt><dd><code>{error.provider_code || '未记录'}</code></dd></div>
    <div><dt>供应商信息</dt><dd>{error.message || '未记录'}</dd></div>
    <div><dt>Request ID</dt><dd>{error.request_id
      ? <Typography.Text code copyable={{ text: error.request_id, tooltips: ['复制 Request ID', '已复制'] }}>{error.request_id}</Typography.Text>
      : '未记录'}</dd></div>
    <div><dt>异常类型</dt><dd>{error.exception_type || '未记录'}</dd></div>
  </dl>
}

export function ModelCalls({ calls }: { calls?: ModelCall[] }) {
  if (!calls?.length) return null
  return <ul className="model-call-list">{calls.map((call, i) => <li key={i}>
    <div>第 {i + 1} 次 · {Number.isFinite(call.duration_ms) ? `${(call.duration_ms / 1000).toFixed(1)} 秒` : '耗时未记录'} · {CALL_OUTCOMES[call.outcome] ?? call.outcome}
    {' · '}结束原因：{call.finish_reason ?? '未提供'}
    {' · '}输出 Token：{call.completion_tokens ?? '未知'}
    {call.reasoning_tokens != null ? ` · 思考 Token：${call.reasoning_tokens}` : ''}</div>
    {call.outcome === 'request_error' && <>
      <RequestErrorDetails error={call.request_error} />
      <p className="diagnostic-note">{call.will_retry === true
        ? `当时安排请求重试${call.retry_delay_seconds != null ? `，等待 ${call.retry_delay_seconds} 秒` : ''}；是否发出后续请求见调用记录。`
        : call.will_retry === false ? '当时未安排请求重试。' : '是否安排请求重试：未记录。'}</p>
    </>}
    {!!call.validation_errors?.length && <ul className="diagnostic-validation">{call.validation_errors.map((issue, index) =>
      <li key={index}><code>{issue.path}</code> · {issue.message} <span>（{issue.code}）</span></li>)}</ul>}
  </li>)}</ul>
}

export default function ModelDiagnostics({ diagnostics }: { diagnostics?: Diagnostics }) {
  if (!diagnostics) return null
  const responses = diagnostics.calls.filter(call => call.outcome !== 'request_error')
  const outputRetries = responses.length ? Math.max(0, ...responses.map(call => call.output_attempt ?? 1)) - 1 : 0
  const hasOutputAttempts = responses.some(call => call.output_attempt !== undefined)
  return <div className="model-diagnostics">
    <p>模型调用 {diagnostics.calls.length} 次 · 输出重试 {hasOutputAttempts ? outputRetries : Math.max(0, responses.length - 1)} 次（不计入补检索或重规划）</p>
    {diagnostics.fallback_used ? <p>规划输出无效，已使用原问题作为检索计划。</p> : null}
    <ModelCalls calls={diagnostics.calls} />
  </div>
}
