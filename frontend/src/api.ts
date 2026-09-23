import type {
  RunFilters,
  RunListResponse,
  RunStatsSummary,
  StoredRunDetail,
  StreamEvent,
  SystemInfo,
} from './types'

const API_PREFIX = '/api/v1'

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

export function getRunDetail(runId: string, signal?: AbortSignal): Promise<StoredRunDetail> {
  return getJson(`${API_PREFIX}/runs/${encodeURIComponent(runId)}`, signal)
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
): Promise<void> {
  const response = await fetch(`${API_PREFIX}/qa/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
    signal,
  })

  if (!response.ok) {
    if (response.status === 422) throw new Error('请输入有效的问题后再运行。')
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
