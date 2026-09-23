export type NodeId = 'planner_v0' | 'retriever_v0' | 'answer_validator_v0'
export type RunStatus = 'idle' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface AgentInfo {
  agent_id: NodeId
  name: string
  role: string
  description: string
}

export interface SystemInfo {
  name: string
  version: string
  protocol_id: string
  model_name: string
  agents: AgentInfo[]
  flow: Array<{ source: string; target: string; condition: string }>
  retry_limits: Record<string, number>
  dataset: {
    name: string
    record_id: string
    question_type: string
    level: string
    context_count: number
    supporting_fact_count: number
    default_question: string
  }
}

export interface SubQuestion {
  id: string
  question: string
  keywords: string[]
  depends_on: string[]
}

export interface EvidenceChunk {
  text: string
  source_title: string
  source_paragraph: number
  relevance_score: number
}

export interface EvidenceGroup {
  sq_id: string
  chunks: EvidenceChunk[]
  sufficiency: 'sufficient' | 'insufficient'
  insufficiency_note: string
}

export interface SupportingEvidence {
  sq_id: string
  text: string
  source_title: string
}

export interface RunResult {
  question?: string
  sub_questions?: SubQuestion[]
  retrieval_strategy?: 'sequential' | 'parallel'
  plan_retry_count?: number
  evidence_chunks?: EvidenceGroup[]
  retrieval_retry_count?: number
  answer?: string
  verified?: boolean
  failure_type?: 'none' | 'insufficient' | 'conflict'
  conflict_flag?: boolean
  conflict_note?: string
  verification_note?: string
  supporting_evidence?: SupportingEvidence[]
  validation_retry_count?: number
}

export interface StreamEvent {
  type:
    | 'run_started'
    | 'node_started'
    | 'node_completed'
    | 'run_completed'
    | 'run_failed'
  run_id: string
  timestamp: string
  payload: Record<string, unknown>
}

export interface TraceStep {
  key: string
  node: NodeId
  cycle: number
  status: 'running' | 'completed'
  timestamp: string
  output?: Partial<RunResult>
}

export interface RunViewState {
  status: RunStatus
  runId?: string
  activeNode?: NodeId
  steps: TraceStep[]
  result?: RunResult
  durationMs?: number
  error?: string
}

export type StoredRunStatus = Exclude<RunStatus, 'idle'>
export type RunEntryPoint = 'api' | 'cli' | 'batch'

export interface RunFilters {
  status?: StoredRunStatus
  entrypoint?: RunEntryPoint
  methodId?: string
  startedFrom?: string
}

export interface StoredRunRecord {
  context: {
    run_id: string
    entrypoint: RunEntryPoint
    question: string
    source_id: string | null
    record_id: string | null
    workflow_id: string
    method_id: string
    model_name: string
    protocol_id: string
    code_version: string
    config_snapshot: Record<string, unknown>
  }
  status: StoredRunStatus
  started_at: string
  finished_at: string | null
  final_state: RunResult | null
  metrics: {
    duration_ms: number | null
    node_call_count: number
    llm_call_count: number | null
    prompt_tokens: number | null
    completion_tokens: number | null
    total_tokens: number | null
    estimated_cost_usd: number | null
    plan_retry_count: number
    retrieval_retry_count: number
    validation_retry_count: number
  }
  error: { error_type: string; message: string } | null
}

export interface StoredTraceEvent {
  run_id: string
  sequence: number
  event_type: string
  timestamp: string
  node: string | null
  cycle: number | null
  duration_ms: number | null
  payload: Record<string, unknown>
}

export interface StoredRunDetail {
  run: StoredRunRecord
  events: StoredTraceEvent[]
}

export interface RunListResponse {
  items: StoredRunRecord[]
  total: number
  limit: number
  offset: number
}

export interface RunStatsSummary {
  total_runs: number
  completed_runs: number
  failed_runs: number
  cancelled_runs: number
  running_runs: number
  verified_runs: number
  completion_rate: number
  verification_pass_rate: number
  average_duration_ms: number | null
  average_retry_count: number | null
  failure_types: Record<string, number>
}
