import type { ModelCall } from './types'

export const CALL_OUTCOMES: Record<string, string> = {
  ok: '结构化输出有效', empty_response: '空正文', truncated_response: '输出被截断',
  invalid_json: 'JSON 格式错误', invalid_schema: '字段格式错误', request_error: '请求失败',
  invalid_citation: '引用标识无效',
}

const REQUEST_CATEGORIES: Record<string, string> = {
  content_filter: '供应商内容审核拦截', billing_or_quota: '账户费用或额度问题',
  rate_limit_or_quota: '请求限流或额度限制', authentication_or_permission: '认证或权限问题',
  timeout: '请求超时', connection_error: '连接错误', provider_service_error: '供应商服务异常',
  bad_request_details_required: '请求被拒绝，需核对错误详情', client_or_transport_error: '客户端或传输异常',
}

export function requestCategoryLabel(category?: string | null): string {
  return category ? REQUEST_CATEGORIES[category] ?? category : '原因未记录'
}

export function errorTypeLabel(type: string): string {
  if (type.startsWith('provider_')) return requestCategoryLabel(type.slice('provider_'.length))
  return ({ model_request_error: '模型请求失败', output_error: '模型输出错误',
    structured_output_error: '模型输出错误', execution_error: '运行异常' } as Record<string, string>)[type] ?? type
}

interface DiagnosticRun {
  status: string
  error?: { error_type: string; message: string } | null
  metrics?: { agent_usage?: Record<string, { calls?: ModelCall[] }> }
  labels?: string[]
}

export function runDiagnosticSummary(run: DiagnosticRun) {
  const groups = Object.values(run.metrics?.agent_usage ?? {}).map(usage => usage.calls ?? [])
  const issues = groups.flat().filter(call => call.outcome !== 'ok')
  const labels = new Set(issues.map(call => call.outcome === 'request_error'
    ? call.request_error?.category ? requestCategoryLabel(call.request_error.category) : '请求失败（详情未记录）'
    : CALL_OUTCOMES[call.outcome] ?? call.outcome))
  if (!labels.size && run.error) labels.add(errorTypeLabel(run.error.error_type))
  if (!labels.size && run.labels?.includes('structured_output_error')) labels.add('模型输出错误')
  const hasIssues = issues.length > 0 || !!run.error || labels.size > 0 || ['failed', 'interrupted', 'cancelled'].includes(run.status)
  // A planned retry alone does not prove another request was sent or recovered.
  const recovered = run.status === 'completed' && groups.some(calls => calls.some((call, index) =>
    call.outcome !== 'ok' && (call.outcome !== 'request_error' || call.will_retry === true)
      && calls.slice(index + 1).some(later => later.outcome === 'ok')))
  const heading = run.status === 'failed' ? '任务失败' : run.status === 'interrupted' ? '状态不明'
    : run.status === 'cancelled' ? '已取消' : recovered ? '重试后完成'
      : hasIssues ? '已记录异常' : '无异常记录'
  return { hasIssues, labels: [...labels], heading, recovered,
    color: run.status === 'failed' ? 'error' : 'warning' }
}
