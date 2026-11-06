import type { StoredRunRecord } from './types'

export interface QuestionBank {
  bank_id: string; version: number; name: string; description: string; count: number
  labelled: number; contextual: number; sha256: string
  preview: { _id: string; question: string; answer: string | null }[]
}
export interface EvaluationOptions {
  methods: string[]; systems: {
    system_id: string; version: string; name: string; available?: boolean; unavailable_reason?: string | null
  }[]
}
export interface EvaluationResult {
  run_id: string; sample_id: string; method_id: string; repeat: number; attempt: number
  question: string; answer: string; status: string; scores: Record<string, number> | null
  error: { message: string; error_type: string } | null
  metrics?: Partial<StoredRunRecord['metrics']>
  labels?: string[]
}
export interface MethodSummary {
  timing_breakdown?: Record<string, { mean_ms: number; reported: number; attempted: number }>
  planned: number; attempted: number; complete: boolean; statuses: Record<string, number>
  scores: Record<string, { value: number | null; scored: number; observed_mean: number | null }>
  usage: Record<string, { reported_total: number | null; reported: number; attempted: number; mean_reported: number | null; median_reported?: number | null; p95_reported?: number | null }>
}
export interface EvaluationManifest {
  manifest_id: string; name: string; purpose: 'smoke' | 'development' | 'evaluation'; bank_id: string
  bank_version: number; bank_sha256: string; count: number; sha256: string; type_counts: Record<string, number>
  samples: { sample_id: string; type: string }[]
}
export interface EvaluationPreflight {
  token: string; planned_tasks: number; sample_count: number; data_sha256: string; purpose: string
  reference_method: string; manifest: EvaluationManifest | null; request: Record<string, unknown>; model_calls: number
  targets: { method_id: string; label: string; models: { agent_id: string; model: { model_name?: string; temperature?: number; max_tokens?: number; system_prompt?: string } | null }[]; limits: Record<string, number> | null; workflow: unknown }[]
}
export interface ResearchReport {
  complete: boolean; primary_metric: string; reference_method: string
  bootstrap: { seed: number; replicates: number; unit: string; confidence: number }; notes: string[]
  methods: Record<string, { by_type: Record<string, MethodSummary>; by_repeat: Record<string, MethodSummary>; repeat_score_std: Record<string, number | null> }>
  pairs: { left: string; right: string; paired_samples: number; planned_samples: number; complete: boolean
    metrics: Record<string, { delta: number; ci95_sample_bootstrap: number[]; left_wins: number; right_wins: number; ties: number } | null> }[]
  reference_pairs?: ResearchReport['pairs']
}
export interface EvaluationJob {
  job_id: string; name: string; status: string; error: string | null; bank: Pick<QuestionBank, 'bank_id' | 'version' | 'name' | 'sha256'>
  context_mode: string; sample_count: number; repeats: number; fingerprint: string; retryable_count: number
  targets: { method_id: string; label: string }[]
  progress: { attempted: number; planned: number; completed: number; failed: number; interrupted: number; cancelled?: number }
  plan?: { request: Record<string, unknown>; manifest: EvaluationManifest | null; reference_method?: string }
  archived?: boolean; readonly?: boolean
  report: { complete: boolean; methods: Record<string, MethodSummary>; retry_count: number; research?: ResearchReport;
    latest_attempt_supplement?: Record<string, MethodSummary>; all_attempt_usage?: MethodSummary['usage'] } | null
}
export interface SavedEvaluationPlan {
  id: string; name: string; request: Record<string, unknown>; sha256: string; source_job_id: string | null; created_at: string
}
export interface EvaluationMonitor {
  heartbeat_at?: string | null
  heartbeat_stale?: boolean
  activities?: import('./types').RunActivity['activities']
  sampled_at: string; last_event_at: string | null; completed_attempt_seconds: number; estimated_remaining_seconds: number | null
  failure_labels: Record<string, number>
  methods: { method_id: string; planned: number; completed: number; failed: number; unresolved: number; unstarted: number; in_progress?: number }[]
  current: { run_id: string; sample_id: string; method_id: string; repeat: number; attempt: number; question: string
    elapsed_seconds: number; status: string; phases: { node: string | null; stage_id: string | null; agent_name: string | null }[] } | null
}
export interface EvaluationArchive {
  archive_id: string; source_key: string; fingerprint: string; imported_at: string; view: EvaluationJob
  original_report: Record<string, unknown>; provenance: Record<string, unknown>; config: Record<string, unknown>
}

export async function evaluationRequest<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(`/api/v1/evaluations${path}`, { method,
    ...(body instanceof FormData ? { body } : body === undefined ? {} : {
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  })
  if (!response.ok) {
    let message = `评测请求失败（HTTP ${response.status}）`
    try {
      const detail = (await response.json()).detail
      if (typeof detail === 'string') message = detail
      else if (Array.isArray(detail)) message = detail.slice(0, 5).map(d =>
        `${Array.isArray(d.loc) ? d.loc.filter((part: unknown) => part !== 'body').join('.') : '参数'}：${String(d.msg ?? '参数无效')}`).join('；')
    } catch { /* Keep the HTTP message for non-JSON failures. */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}
