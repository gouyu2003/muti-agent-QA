export type NodeId = string
export type AgentType = 'planner' | 'retriever' | 'answer_validator' | 'expert'
export interface AgentInstance {
  agent_id: string
  agent_type: AgentType
  name: string
  description: string
  model_config: { model_name: string; system_prompt: string; temperature: number; max_tokens: number } | null
  source_id: string | null
  source_version: number | null
  top_k: number
}
export interface AgentTemplate {
  agent_type: AgentType
  name: string
  uses_llm: boolean
  uses_knowledge: boolean
  editable_fields: string[]
  defaults: AgentInstance
}
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

export interface WorkflowOption {
  execution_mode?: 'sequential' | 'parallel'
  workflow_id: string
  name: string
  description: string
  method_id: string
  entry_node: string
  agents: string[]
  edges: Array<{ source: string; target: string; route_name: string }>
  retry_limits: Record<string, number>
}

export interface SourceOption {
  source_id: string
  name: string
  source_type: string
  is_demo: boolean
  record_id: string | null
  question_type: string | null
  level: string | null
  context_count: number | null
  supporting_fact_count: number | null
  example_question: string | null
  version?: number | null
  versions?: number[]
  file_count?: number | null
  chunk_count?: number | null
  version_details?: Array<{ version: number; file_count: number; chunk_count: number }>
}

export interface KnowledgeChunk {
  text: string
  document_id: string
  chunk_id: string
  filename: string
  heading_path: string[]
  line_start: number
  line_end: number
  char_start: number
  char_end: number
}

export interface KnowledgeVersion {
  version: number
  status: 'importing' | 'ready' | 'failed'
  created_at: string
  file_count?: number
  chunk_count?: number
  parser_version?: string
  index_version?: string
  files?: Array<{ filename: string; size_bytes: number; sha256: string }>
  error: { code: string; message: string } | null
}

export interface KnowledgeSource {
  source_id: string
  name: string
  status: 'importing' | 'ready' | 'failed'
  current_version: number | null
  file_count: number
  chunk_count: number
  versions: KnowledgeVersion[]
}

export interface ChunkPage {
  items: KnowledgeChunk[]
  total: number
  limit: number
  offset: number
}

export interface EvidenceLocation {
  evidence_id?: string
  source_version?: number
  document_id?: string
  chunk_id?: string
  filename?: string
  heading_path?: string[]
  line_start?: number
  line_end?: number
  char_start?: number
  char_end?: number
}

export interface SystemOptions {
  system_id: string
  system_version: string
  default_workflow_id: string
  default_source_id: string
  workflows: WorkflowOption[]
  sources: SourceOption[]
}

export interface SubQuestion {
  id: string
  question: string
  keywords: string[]
  depends_on: string[]
}

export interface EvidenceChunk extends EvidenceLocation {
  text: string
  source_title: string
  source_paragraph?: number
  relevance_score: number
  source_id?: string
}

export interface EvidenceGroup {
  sq_id: string
  chunks: EvidenceChunk[]
  sufficiency: 'sufficient' | 'insufficient' | 'unknown'
  insufficiency_note: string
}

export interface SupportingEvidence extends EvidenceLocation {
  sq_id: string
  text: string
  source_title: string
  source_paragraph?: number
  source_id?: string
}

export interface ModelCall {
  attempt: number
  duration_ms: number
  finish_reason?: string | null
  completion_tokens?: number | null
  reasoning_tokens?: number | null
  output_chars: number
  outcome: string
}

export interface ModelDiagnostics {
  calls: ModelCall[]
  retry_count: number
  output_error?: string | null
  fallback_used?: boolean
}

export interface RunResult {
  model_diagnostics?: ModelDiagnostics
  answer_scope?: 'complete' | 'partial' | 'none'
  missing_information?: string[]
  expert_results?: Record<string, RunResult & { agent_name?: string }>
  question?: string
  sub_questions?: SubQuestion[]
  retrieval_strategy?: 'sequential' | 'parallel'
  plan_retry_count?: number
  evidence_chunks?: EvidenceGroup[]
  retrieval_retry_count?: number
  answer?: string
  verified?: boolean
  failure_type?: 'none' | 'insufficient' | 'conflict' | 'output_error'
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
    | 'run_cancelled'
  run_id: string
  timestamp: string
  payload: Record<string, unknown>
}

export interface TraceStep {
  key: string
  node: NodeId
  agentName?: string
  agentType?: string
  cycle: number
  status: 'running' | 'completed' | 'failed' | 'cancelled'
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
  experimentId?: string
  systemId?: string
}

export interface SystemDraftFields {
  agents?: AgentInstance[]
  execution_mode?: 'sequential' | 'parallel'
  name: string
  description: string
  workflow_id: string | null
  source_id: string | null
  source_version: number | null
}

export interface SavedSystemSnapshot {
  schema_version?: string
  instances?: AgentInstance[]
  execution_mode?: 'sequential' | 'parallel'
  agent_types?: Record<string, string>
  configuration_errors?: string[]
  sources?: Record<string, { source_id: string; name: string; version?: number; sha256: string; index_sha256?: string }>
  config_sha256: string
  agents: Record<string, { agent_name: string; role: string; description?: string; retrieval_config?: { top_k: number }; model_config: { model_name: string; system_prompt: string; temperature: number; max_tokens: number } }>
  workflow_spec: { protocol_id: string; method_id: string; entry_node: string; agents: string[]; edges: { from: string; to: string; route_name: string }[]; max_cycles: Record<string, number> } | null
  protocol: { protocol_name?: string; description?: string }
  source?: { source_id: string; source_type: string; name: string; version?: number; file_count?: number; chunk_count?: number }
}

export interface SystemDocument extends SystemDraftFields {
  snapshot: SavedSystemSnapshot
  version?: string
  draft_revision?: number
  created_at?: string
}

export interface ManagedSystem {
  system_id: string
  draft_revision: number
  draft: SystemDocument
  versions: SystemDocument[]
  created_at: string
  updated_at: string
}

export interface RunSelection {
  workflowId?: string
  sourceId?: string
  sourceVersion?: number
  systemId?: string
  systemVersion?: string
  draftRevision?: number
}

export interface ManagedSelection extends RunSelection {
  name: string
  document: SystemDocument
}

export interface ExperimentOption {
  experiment_id: string
  name: string
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
    experiment_id?: string | null
    sample_id?: string | null
  }
  status: StoredRunStatus
  started_at: string
  finished_at: string | null
  final_state: RunResult | null
  metrics: {
    agent_usage?: Record<string, { call_count: number; total_tokens: number | null; calls?: ModelCall[] }>
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
