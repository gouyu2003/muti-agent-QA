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
