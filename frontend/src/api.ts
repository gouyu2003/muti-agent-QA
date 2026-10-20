import type {
  RunFilters,
  RunListResponse,
  RunStatsSummary,
  StoredRunDetail,
  StreamEvent,
  SystemInfo,
  SystemOptions,
  KnowledgeSource,
  ChunkPage,
  ExperimentOption,
  ManagedSystem, SystemDocument, SystemDraftFields, RunSelection,
  AgentTemplate,
} from './types'

const API_PREFIX = '/api/v1'

export function getAgentTemplates(signal?: AbortSignal): Promise<{ items: AgentTemplate[] }> {
  return knowledgeJson(`${API_PREFIX}/agent-templates`, { signal })
}

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal })
  if (!response.ok) {
    if (response.status === 404) throw new Error('这条运行记录不存在或已被移除。')
    throw new Error(`运行历史加载失败（HTTP ${response.status}）`)
  }
  return response.json() as Promise<T>
}

function historyQuery(filters: RunFilters): URLSearchParams {
  const query = new URLSearchParams()
  if (filters.status) query.set('status', filters.status)
  if (filters.entrypoint) query.set('entrypoint', filters.entrypoint)
  if (filters.methodId) query.set('method_id', filters.methodId)
  if (filters.startedFrom) query.set('started_from', filters.startedFrom)
  if (filters.experimentId) query.set('experiment_id', filters.experimentId)
  if (filters.systemId) query.set('system_id', filters.systemId)
  return query
}

export function getRuns(
  filters: RunFilters,
  limit: number,
  offset: number,
  signal?: AbortSignal,
): Promise<RunListResponse> {
  const query = historyQuery(filters)
  query.set('limit', String(limit))
  query.set('offset', String(offset))
  return getJson(`${API_PREFIX}/runs?${query}`, signal)
}

export function getRunDetail(runId: string, signal?: AbortSignal, experimentId?: string): Promise<StoredRunDetail> {
  const query = historyQuery({ experimentId })
  return getJson(`${API_PREFIX}/runs/${encodeURIComponent(runId)}${query.size ? `?${query}` : ''}`, signal)
}

export function getExperiments(signal?: AbortSignal): Promise<ExperimentOption[]> {
  return getJson(`${API_PREFIX}/experiments`, signal)
}

export function getRunStats(filters: RunFilters, signal?: AbortSignal): Promise<RunStatsSummary> {
  const query = historyQuery(filters)
  return getJson(`${API_PREFIX}/stats/summary?${query}`, signal)
}

export function getRunsExportUrl(filters: RunFilters): string {
  const query = historyQuery(filters)
  return `${API_PREFIX}/exports/runs.jsonl${query.size ? `?${query}` : ''}`
}

export async function getSystemInfo(signal?: AbortSignal): Promise<SystemInfo> {
  const response = await fetch(`${API_PREFIX}/system`, { signal })
  if (!response.ok) {
    throw new Error(`系统信息加载失败（HTTP ${response.status}）`)
  }
  return response.json() as Promise<SystemInfo>
}

export async function getSystemOptions(signal?: AbortSignal): Promise<SystemOptions> {
  const response = await fetch(`${API_PREFIX}/system/options`, { signal })
  if (!response.ok) throw new Error(`配置选项加载失败（HTTP ${response.status}）`)
  return response.json() as Promise<SystemOptions>
}

export class KnowledgeImportError extends Error {
  sourceId?: string
  version?: number
  constructor(message: string, sourceId?: string, version?: number) {
    super(message)
    this.sourceId = sourceId
    this.version = version
  }
}

async function knowledgeJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const data = await response.json()
  if (!response.ok) {
    const detail = data.detail
    throw new KnowledgeImportError(
      typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(item => `${item.loc?.join(' / ')}：${item.msg}`).join('；') : typeof detail === 'object' && detail?.message ? detail.message : `请求失败（HTTP ${response.status}）`,
      detail?.source_id, detail?.version,
    )
  }
  return data as T
}

export function getKnowledgeSources(signal?: AbortSignal): Promise<{ items: KnowledgeSource[] }> {
  return knowledgeJson(`${API_PREFIX}/knowledge/sources`, { signal })
}

export function deleteKnowledgeSource(sourceId: string): Promise<{ source_id: string; deleted: boolean }> {
  return knowledgeJson(`${API_PREFIX}/knowledge/sources/${encodeURIComponent(sourceId)}`, { method: 'DELETE' })
}

export function getManagedSystems(signal?: AbortSignal): Promise<{ items: ManagedSystem[] }> {
  return knowledgeJson(`${API_PREFIX}/systems`, { signal })
}

export function getManagedSystem(id: string): Promise<ManagedSystem> {
  return knowledgeJson(`${API_PREFIX}/systems/${encodeURIComponent(id)}`)
}

export function saveManagedSystem(fields: SystemDraftFields, id?: string, revision?: number): Promise<ManagedSystem> {
  return knowledgeJson(`${API_PREFIX}/systems${id ? `/${encodeURIComponent(id)}/draft` : ''}`, {
    method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...fields, ...(id ? { expected_revision: revision } : {}) }),
  })
}

export function publishManagedSystem(id: string, revision: number): Promise<SystemDocument> {
  return knowledgeJson(`${API_PREFIX}/systems/${encodeURIComponent(id)}/versions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: revision }),
  })
}

export function getKnowledgeSource(sourceId: string, signal?: AbortSignal): Promise<KnowledgeSource> {
  return knowledgeJson(`${API_PREFIX}/knowledge/sources/${encodeURIComponent(sourceId)}`, { signal })
}

export function getKnowledgeChunks(sourceId: string, version: number, offset = 0, signal?: AbortSignal): Promise<ChunkPage> {
  return knowledgeJson(`${API_PREFIX}/knowledge/sources/${encodeURIComponent(sourceId)}/versions/${version}/chunks?limit=10&offset=${offset}`, { signal })
}

export function importKnowledge(name: string, files: File[], sourceId?: string): Promise<KnowledgeSource> {
  const body = new FormData()
  if (!sourceId) body.append('name', name)
  files.forEach((file) => body.append('files', file))
  const suffix = sourceId ? `/${encodeURIComponent(sourceId)}/versions` : ''
  return knowledgeJson(`${API_PREFIX}/knowledge/sources${suffix}`, { method: 'POST', body })
}

export function parseSseBlock(block: string): StreamEvent | null {
  let eventType = ''
  const dataLines: string[] = []

  for (const rawLine of block.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (line.startsWith('event:')) {
      eventType = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart())
    }
  }

  if (!eventType || dataLines.length === 0) return null
  const envelope = JSON.parse(dataLines.join('\n')) as Omit<StreamEvent, 'type'>
  return { type: eventType as StreamEvent['type'], ...envelope }
}

export async function streamQuestion(
  question: string,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
  selection?: RunSelection,
): Promise<void> {
  const response = await fetch(`${API_PREFIX}/qa/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, workflow_id: selection?.workflowId, source_id: selection?.sourceId, source_version: selection?.sourceVersion,
      system_id: selection?.systemId, system_version: selection?.systemVersion, draft_revision: selection?.draftRevision }),
    signal,
  })

  if (!response.ok) {
    if ([404, 409, 422].includes(response.status)) {
      const data = await response.json().catch(() => null) as { detail?: unknown } | null
      const detail = typeof data?.detail === 'string' ? data.detail : '问题或所选配置无效。'
      throw new Error(detail)
    }
    throw new Error(`问答服务请求失败（HTTP ${response.status}）`)
  }
  if (!response.body) throw new Error('浏览器未收到可读取的流式响应。')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')

    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const event = parseSseBlock(block)
      if (event) onEvent(event)
      boundary = buffer.indexOf('\n\n')
    }

    if (done) break
  }

  const tail = buffer.trim()
  if (tail) {
    const event = parseSseBlock(tail)
    if (event) onEvent(event)
  }
}
