import { describe, expect, it } from 'vitest'
import { initialRunState, runReducer } from './runState'
import type { StreamEvent } from './types'

function event(type: StreamEvent['type'], payload: Record<string, unknown>): StreamEvent {
  return { type, run_id: 'r1', timestamp: 'now', payload }
}

describe('runReducer', () => {
  it('matches concurrent calls of one instance by invocation ID and keeps stage identities', () => {
    let state = runReducer(initialRunState, { type: 'reset' })
    for (const call_id of ['call-a', 'call-b']) state = runReducer(state, { type: 'stream_event', event: event('node_started', {
      node: 'expert_one', stage_id: 'assigned_work', cycle: 1, call_id, task_id: call_id,
    }) })
    state = runReducer(state, { type: 'stream_event', event: event('node_completed', {
      node: 'expert_one', call_id: 'call-a', output: { answer: 'First task' },
    }) })
    expect(state.steps[0].status).toBe('completed')
    expect(state.steps[1].status).toBe('running')
    expect(state.steps[0].stageId).toBe('assigned_work')
    state = runReducer(state, { type: 'stream_event', event: event('node_completed', {
      node: 'expert_one', call_id: 'call-b', output: { answer: 'Second task' },
    }) })
    expect(state.steps.map(s => s.output?.answer)).toEqual(['First task', 'Second task'])
    state = runReducer(state, { type: 'stream_event', event: event('route_decided', { stage_id: 'assigned_work', target: 'lead', reason: 'always' }) })
    expect(state.routes).toHaveLength(1)
  })
  it('keeps both dynamic parallel instances visible until each completes', () => {
    let state = runReducer(initialRunState, { type: 'reset' })
    for (const node of ['medical_a', 'medical_b']) state = runReducer(state, { type: 'stream_event',
      event: event('node_started', { node, agent_type: 'expert', agent_name: node, cycle: 1 }) })
    expect(state.steps.filter(s => s.status === 'running')).toHaveLength(2)
    state = runReducer(state, { type: 'stream_event', event: event('node_completed', { node: 'medical_b', output: { expert_results: { medical_b: { answer: 'B' } } } }) })
    expect(state.activeNode).toBe('medical_a')
    expect(state.steps[1].agentType).toBe('expert')
    expect(state.steps[1].output?.expert_results?.medical_b.answer).toBe('B')
    state = runReducer(state, { type: 'stream_event', event: event('node_completed', { node: 'medical_a', output: {} }) })
    expect(state.steps.every(s => s.status === 'completed')).toBe(true)
    expect(state.activeNode).toBeUndefined()
  })
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
