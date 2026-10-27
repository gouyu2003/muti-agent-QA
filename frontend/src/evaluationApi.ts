export interface QuestionBank {
  bank_id: string; version: number; name: string; description: string; count: number
  labelled: number; contextual: number; sha256: string
  preview: { _id: string; question: string; answer: string | null }[]
}
export interface EvaluationOptions {
  methods: string[]; systems: { system_id: string; version: string; name: string }[]
}
export interface EvaluationResult {
  run_id: string; sample_id: string; method_id: string; repeat: number; attempt: number
  question: string; answer: string; status: string; scores: Record<string, number> | null
  error: { message: string; error_type: string } | null
}
export interface MethodSummary {
  planned: number; attempted: number; complete: boolean; statuses: Record<string, number>
  scores: Record<string, { value: number | null; scored: number; observed_mean: number | null }>
  usage: Record<string, { reported_total: number | null; reported: number; attempted: number; mean_reported: number | null }>
}
export interface EvaluationJob {
  job_id: string; name: string; status: string; error: string | null; bank: Pick<QuestionBank, 'bank_id' | 'version' | 'name' | 'sha256'>
  context_mode: string; sample_count: number; repeats: number; fingerprint: string
  targets: { method_id: string; label: string }[]
  progress: { attempted: number; planned: number; completed: number; failed: number; interrupted: number; cancelled?: number }
  report: { complete: boolean; methods: Record<string, MethodSummary>; retry_count: number } | null
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
    } catch { /* Keep the HTTP message for non-JSON failures. */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}
