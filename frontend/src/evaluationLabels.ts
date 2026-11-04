import type { EvaluationJob } from './evaluationApi'

export const METHOD_NAMES: Record<string, string> = {
  single_rag: '单智能体 RAG', pipeline_fixed: '固定流水线', pipeline_adaptive: '反馈流水线',
  multi_expert: '多专家', lead: 'Lead Agent', debate: 'Debate',
}
export function targetLabel(job: EvaluationJob, id: string): string {
  const label = job.targets.find(t => t.method_id === id)?.label ?? id
  return METHOD_NAMES[label] ?? label
}
