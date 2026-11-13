import type { RunResult, SavedSystemSnapshot, StoredRunStatus } from './types'

export interface ChatSelection { system_id: string; system_version?: string; draft_revision?: number }
export interface Conversation {
  conversation_id: string
  title: string
  created_at: string
  updated_at: string
  preview: string
  active_run_id: string | null
  system_ref: { system_id: string; name: string; kind: string; system_version?: string; draft_revision?: number }
  config_snapshot?: SavedSystemSnapshot
  chat_policy_version?: string
}
export interface ChatContext {
  intent: string
  resolved_question?: string
  clarification?: string
  context_status?: string
  used_message_ids?: string[]
}
export interface ChatMessage {
  message_id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  turn_index: number
  sequence: number
  run_id: string
  status: StoredRunStatus
  error: string | null
  context: ChatContext
  result: Partial<RunResult>
  created_at: string
}
export interface PendingChat {
  createId: string
  requestId: string
  conversationId?: string
  question: string
  selection: ChatSelection
}
export class ChatApiError extends Error {
  constructor(message: string, public status: number, public activeRunId?: string) { super(message) }
}
const base = '/api/v1/conversations'
async function json<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${base}${path}`, { method, signal,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) })
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    throw new ChatApiError(typeof data.detail === 'string' ? data.detail : `聊天请求失败（HTTP ${response.status}）`, response.status, data.active_run_id)
  }
  return response.status === 204 ? undefined as T : response.json()
}
const path = (id: string) => `/${encodeURIComponent(id)}`
export const listConversations = (signal?: AbortSignal) => json<{ items: Conversation[] }>('', 'GET', undefined, signal)
export const getConversation = (id: string, signal?: AbortSignal) => json<Conversation>(path(id), 'GET', undefined, signal)
export const getChatMessages = (id: string, signal?: AbortSignal) => json<{ items: ChatMessage[] }>(`${path(id)}/messages`, 'GET', undefined, signal)
export const createConversation = (selection: ChatSelection, requestId: string) => json<Conversation>('', 'POST', { ...selection, client_request_id: requestId })
export const renameConversation = (id: string, title: string) => json<Conversation>(path(id), 'PATCH', { title })
export const deleteConversation = (id: string) => json<void>(path(id), 'DELETE')
export const sendChatMessage = (id: string, question: string, requestId: string) => json<{ run_id: string; conversation_id: string }>(`${path(id)}/messages`, 'POST', { question, client_request_id: requestId })
const pendingKey = (id?: string) => `maqa.chat.pending.${id || 'new'}`
export function readPendingChat(id?: string): PendingChat | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(pendingKey(id)) || 'null')
    return value && typeof value.question === 'string' && typeof value.requestId === 'string'
      && typeof value.createId === 'string' && typeof value.selection?.system_id === 'string' ? value : undefined
  } catch { return undefined }
}
export function savePendingChat(pending: PendingChat, originalId?: string) {
  localStorage.setItem(pendingKey(originalId), JSON.stringify(pending))
  if (pending.conversationId) localStorage.setItem(pendingKey(pending.conversationId), JSON.stringify(pending))
}
export function clearPendingChat(pending: PendingChat, originalId?: string) {
  for (const id of [originalId, pending.conversationId]) {
    if (readPendingChat(id)?.requestId === pending.requestId) localStorage.removeItem(pendingKey(id))
  }
}
