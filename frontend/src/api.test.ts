import { describe, expect, it, vi } from 'vitest'
import { getRuns, getRunsExportUrl, getRunStats, getRunDetail, parseSseBlock, streamQuestion, importKnowledge } from './api'

describe('parseSseBlock', () => {
  it('parses an event and preserves Chinese payload text', () => {
    const event = parseSseBlock(
      'event: run_failed\ndata: {"run_id":"r1","timestamp":"now","payload":{"message":"配置缺失"}}',
    )

    expect(event).toEqual({
      type: 'run_failed',
      run_id: 'r1',
      timestamp: 'now',
      payload: { message: '配置缺失' },
    })
  })

  it('ignores incomplete blocks', () => {
    expect(parseSseBlock('data: {}')).toBeNull()
    expect(parseSseBlock('event: run_started')).toBeNull()
  })
})

describe('history API', () => {
  it('uses the same filters for the list and JSONL export', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 0, limit: 10, offset: 0 }),
    } as Response)
    const filters = {
      status: 'completed' as const,
      entrypoint: 'api' as const,
      methodId: 'multi_agent_adaptive_v0',
      startedFrom: '2026-09-22T08:00:00.000Z',
      experimentId: 'smoke-v1-validated',
      systemId: 'sys_a',
    }

    try {
      await getRuns(filters, 10, 20)
      const listUrl = new URL(fetchMock.mock.calls[0][0] as string, 'http://localhost')
      const exportUrl = new URL(getRunsExportUrl(filters), 'http://localhost')
      await getRunStats(filters)
      const statsUrl = new URL(fetchMock.mock.calls[1][0] as string, 'http://localhost')
      await getRunDetail('run-1', undefined, filters.experimentId)
      expect(new URL(fetchMock.mock.calls[2][0] as string, 'http://localhost').searchParams.get('experiment_id')).toBe(filters.experimentId)
      for (const key of ['status', 'entrypoint', 'method_id', 'started_from', 'experiment_id', 'system_id']) {
        expect(listUrl.searchParams.get(key)).toBe(exportUrl.searchParams.get(key))
        expect(listUrl.searchParams.get(key)).toBe(statsUrl.searchParams.get(key))
      }
      expect(listUrl.searchParams.get('offset')).toBe('20')
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('configured stream API', () => {
  it('sends an explicit system version without temporary source overrides', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => ({
      ok: true, body: new ReadableStream({ start(controller) { controller.close() } }),
    } as Response))
    try {
      await streamQuestion('Q?', () => {}, undefined, { systemId: 'sys_a', systemVersion: 'v1' })
      expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({ question: 'Q?', system_id: 'sys_a', system_version: 'v1' })
      await streamQuestion('Q?', () => {}, undefined, { systemId: 'sys_a', draftRevision: 3 })
      expect(JSON.parse((fetchMock.mock.calls[1][1] as RequestInit).body as string)).toEqual({ question: 'Q?', system_id: 'sys_a', draft_revision: 3 })
    } finally { fetchMock.mockRestore() }
  })
  it('sends the selected workflow and knowledge source IDs', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      body: new ReadableStream({ start(controller) { controller.close() } }),
    } as Response)
    try {
      await streamQuestion('Question?', () => {}, undefined, {
        workflowId: 'multi_hop_qa_fixed', sourceId: 'demo_fixture_002',
      })
      expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/qa/stream')
      expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({
        question: 'Question?', workflow_id: 'multi_hop_qa_fixed', source_id: 'demo_fixture_002',
      })
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('uploads files as multipart without forcing a JSON content type', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, json: async () => ({ source_id: 'user_a' }) } as Response)
    try {
      const file = new File(['English'], 'a.txt')
      await importKnowledge('Notes', [file])
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect(init.headers).toBeUndefined()
      expect((init.body as FormData).get('name')).toBe('Notes')
      expect((init.body as FormData).getAll('files')).toHaveLength(1)
    } finally { fetchMock.mockRestore() }
  })
})
