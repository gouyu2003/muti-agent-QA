export type ProtocolKind = 'plan' | 'retrieve' | 'propose' | 'critique' | 'revise' | 'lead' | 'dispatch' | 'review' | 'answer'
export interface ProtocolSpec {
  language_version: '1' | '2'; family: 'pipeline' | 'lead' | 'debate' | 'single_expert' | 'multi_expert'
  retry_limits?: { retrieval: number; planning: number }
  slots: Record<string, { role: string; minimum: number; maximum: number }>
  entry_stage: string
  stages: { stage_id: string; kind: ProtocolKind; slot: string; mode: 'sequential' | 'parallel'; reads: string[];
    routes: { condition: string; target: string }[]; max_visits: number; on_limit: string; instructions: string; share_evidence: boolean; peer_policy?: 'all' | 'next' }[]
  limits: { max_node_calls: number; max_llm_calls: number; max_reserved_tokens: number; timeout_seconds: number; node_timeout_seconds: number }
}
export interface ProtocolCapabilities {
  stage_kinds: ProtocolKind[]; conditions: string[]; roles: string[]; min_stages: number; max_stages: number
  max_visits: number; max_routes: number; max_slot_size: number; max_retry_count: number
  parallel_kinds: ProtocolKind[]; final_kinds: ProtocolKind[]; limit_target_kinds: ProtocolKind[]
  conditions_by_kind: Record<string, string[]>; family_kinds: Record<string, ProtocolKind[]>; family_min_stages: Record<string, number>
}
export interface ProtocolCatalog {
  items: { template_id: string; name: string; description: string; spec: ProtocolSpec }[]
  capabilities: Record<string, ProtocolCapabilities>
}
export interface ProtocolDocument { protocol_id: string; revision: number; name: string; description: string; spec: ProtocolSpec; spec_sha256: string; version?: string }
export interface ManagedProtocol { protocol_id: string; draft_revision: number; archived: boolean; draft: ProtocolDocument; versions: ProtocolDocument[] }
export interface ProtocolRef { protocol_id: string; version?: string; revision?: number }
export interface ProtocolCall {
  call_id: string; agent_id: string; agent_name: string; stage_id: string; round: number; task_id: string; kind: string; invocation_index: number; status: string
  input: { question: string; task: { instruction: string }; reads: unknown[] }
  usage: { call_count: number; total_tokens?: number | null }
  output: import('./types').RunResult & {
    error?: string
    action?: string; summary?: string; reason?: string; change_note?: string
    tasks?: { task_id: string; agent_id: string; instruction: string }[]
    critiques?: { target_call_id: string; stance: string; issue: string; evidence_ids: string[] }[]
    responses?: { critique_call_id: string; accepted: boolean; response: string }[]
  }
}
