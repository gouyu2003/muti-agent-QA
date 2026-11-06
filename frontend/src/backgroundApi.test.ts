import { afterEach, describe, expect, it, vi } from 'vitest'
import { getPendingRun, resumeBackgroundRun, streamQuestion, cancelBackgroundRun } from './api'
import type { StreamEvent } from './types'

afterEach(() => { vi.restoreAllMocks(); localStorage.clear() })
function eventsResponse(events: string) {
  return { ok: true, body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(events)); controller.close() } }) } as Response
}
const complete = 'id: 0\nevent: run_started\ndata: {"run_id":"r1","timestamp":"now","payload":{}}\n\nid: 1\nevent: run_completed\ndata: {"run_id":"r1","timestamp":"now","payload":{"result":{"answer":"完成"}}}\n\n'

describe('background execution transport', () => {
  it('submits once, persists the run ID and subscribes without a second model request', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({ ok: true, json: async () => ({ run_id: 'r1', status: 'queued' }) } as Response)
      .mockImplementationOnce(async () => {
        expect(getPendingRun()?.runId).toBe('r1')
        return eventsResponse(complete)
      })
    const events: StreamEvent[] = []
    await streamQuestion('中文问题', event => events.push(event), undefined, { systemId: 's1', systemVersion: 'v2' })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/qa/runs')
    expect(JSON.parse(fetchMock.mock.calls[0][1]!.body as string)).toMatchObject({ question: '中文问题', system_id: 's1', system_version: 'v2' })
    expect(fetchMock.mock.calls[1][0]).toContain('/runs/r1/events/stream?after_sequence=-1')
    expect(events.at(-1)?.type).toBe('run_completed')
    expect(getPendingRun()).toBeUndefined()
  })

  it('restores an existing run by subscribing and sends no submission', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(eventsResponse(complete))
    await resumeBackgroundRun({ question: 'Q', clientRequestId: 'same-key', runId: 'r1' }, () => {})
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toContain('/runs/r1/events/stream')
  })

  it('keeps an unacknowledged submission key for an idempotent retry', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('offline'))
    const controller = new AbortController()
    const result = streamQuestion('Q', () => controller.abort(), controller.signal)
    await expect(result).rejects.toThrow('Aborted')
    expect(getPendingRun()).toMatchObject({ question: 'Q', clientRequestId: expect.any(String) })
  })

  it('a stop request returns cancelling, rather than pretending execution already stopped', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, json: async () => ({ run_id: 'r1', status: 'cancelling' }) } as Response)
    expect((await cancelBackgroundRun('r1')).status).toBe('cancelling')
    expect(fetchMock.mock.calls[0][1]?.method).toBe('POST')
  })
})
