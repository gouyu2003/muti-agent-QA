import type { ModelDiagnostics as Diagnostics, ModelCall } from './types'

const outcomes: Record<string, string> = {
  ok: '结构化输出有效', empty_response: '空正文', truncated_response: '输出被截断',
  invalid_json: 'JSON 格式错误', invalid_schema: '字段格式错误', request_error: '请求失败',
  invalid_citation: '引用标识无效',
}

export function ModelCalls({ calls }: { calls?: ModelCall[] }) {
  if (!calls?.length) return null
  return <ul>{calls.map((call, i) => <li key={i}>
    第 {i + 1} 次 · {(call.duration_ms / 1000).toFixed(1)} 秒 · {outcomes[call.outcome] ?? call.outcome}
    {' · '}结束原因：{call.finish_reason ?? '未提供'}
    {' · '}输出 Token：{call.completion_tokens ?? '未知'}
    {call.reasoning_tokens != null ? ` · 思考 Token：${call.reasoning_tokens}` : ''}
  </li>)}</ul>
}

export default function ModelDiagnostics({ diagnostics }: { diagnostics?: Diagnostics }) {
  if (!diagnostics) return null
  return <div className="model-diagnostics">
    <p>模型调用 {diagnostics.calls.length} 次 · 输出重试 {diagnostics.retry_count} 次（不计入补检索或重规划）</p>
    {diagnostics.fallback_used ? <p>规划输出无效，已使用原问题作为检索计划。</p> : null}
    <ModelCalls calls={diagnostics.calls} />
  </div>
}
