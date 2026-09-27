import { describe, expect, it, vi } from 'vitest'
import { getRuns, getRunsExportUrl, parseSseBlock, streamQuestion } from './api'

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
    }

    try {
      await getRuns(filters, 10, 20)
      const listUrl = new URL(fetchMock.mock.calls[0][0] as string, 'http://localhost')
      const exportUrl = new URL(getRunsExportUrl(filters), 'http://localhost')
      for (const key of ['status', 'entrypoint', 'method_id', 'started_from']) {
        expect(listUrl.searchParams.get(key)).toBe(exportUrl.searchParams.get(key))
      }
      expect(listUrl.searchParams.get('offset')).toBe('20')
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('configured stream API', () => {
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
})
