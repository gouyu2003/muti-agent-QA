import { describe, expect, it } from 'vitest'
import { initialRunState, runReducer } from './runState'
import type { StreamEvent } from './types'

function event(type: StreamEvent['type'], payload: Record<string, unknown>): StreamEvent {
  return { type, run_id: 'r1', timestamp: 'now', payload }
}

describe('runReducer', () => {
  it('keeps repeated node cycles as separate trace steps', () => {
    let state = runReducer(initialRunState, { type: 'reset' })
    state = runReducer(state, {
      type: 'stream_event',
      event: event('node_started', { node: 'retriever_v0', cycle: 1 }),
    })
    state = runReducer(state, {
      type: 'stream_event',
      event: event('node_completed', { node: 'retriever_v0', cycle: 1, output: {} }),
    })
    state = runReducer(state, {
      type: 'stream_event',
      event: event('node_started', { node: 'retriever_v0', cycle: 2 }),
    })

    expect(state.steps).toHaveLength(2)
    expect(state.steps.map((step) => step.cycle)).toEqual([1, 2])
    expect(state.activeNode).toBe('retriever_v0')
  })

  it('stores the final result and duration', () => {
    const state = runReducer(initialRunState, {
      type: 'stream_event',
      event: event('run_completed', {
        duration_ms: 1250,
        result: { answer: 'Chief of Protocol', verified: true },
      }),
    })

    expect(state.status).toBe('completed')
    expect(state.durationMs).toBe(1250)
    expect(state.result?.answer).toBe('Chief of Protocol')
  })
})
