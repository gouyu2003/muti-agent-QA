import type { NodeId, RunResult, RunViewState, StreamEvent } from './types'

export const initialRunState: RunViewState = {
  status: 'idle',
  steps: [],
}

export type RunAction =
  | { type: 'reset' }
  | { type: 'cancel' }
  | { type: 'client_error'; message: string }
  | { type: 'stream_event'; event: StreamEvent }

function isNodeId(value: unknown): value is NodeId {
  return value === 'planner_v0' || value === 'retriever_v0' || value === 'answer_validator_v0'
}

export function runReducer(state: RunViewState, action: RunAction): RunViewState {
  if (action.type === 'reset') return { status: 'running', steps: [] }
  if (action.type === 'cancel') {
    return { ...state, status: 'cancelled', activeNode: undefined, error: undefined }
  }
  if (action.type === 'client_error') {
    return { ...state, status: 'failed', activeNode: undefined, error: action.message }
  }

  const { event } = action
  if (event.type === 'run_started') {
    return { ...state, status: 'running', runId: event.run_id }
  }

  if (event.type === 'node_started') {
    const node = event.payload.node
    const cycle = Number(event.payload.cycle ?? 1)
    if (!isNodeId(node)) return state
    return {
      ...state,
      activeNode: node,
      steps: [
        ...state.steps,
        {
          key: `${node}-${cycle}-${state.steps.length}`,
          node,
          cycle,
          status: 'running',
          timestamp: event.timestamp,
        },
      ],
    }
  }

  if (event.type === 'node_completed') {
    const node = event.payload.node
    const cycle = Number(event.payload.cycle ?? 1)
    if (!isNodeId(node)) return state
    const index = [...state.steps]
      .map((step, itemIndex) => ({ step, itemIndex }))
      .reverse()
      .find(({ step }) => step.node === node && step.status === 'running')?.itemIndex
    const output = (event.payload.output ?? {}) as Partial<RunResult>
    const steps = [...state.steps]
    if (index === undefined) {
      steps.push({
        key: `${node}-${cycle}-${steps.length}`,
        node,
        cycle,
        status: 'completed',
        timestamp: event.timestamp,
        output,
      })
    } else {
      steps[index] = { ...steps[index], status: 'completed', output }
    }
    return { ...state, steps }
  }

  if (event.type === 'run_completed') {
    return {
      ...state,
      status: 'completed',
      activeNode: undefined,
      durationMs: Number(event.payload.duration_ms ?? 0),
      result: (event.payload.result ?? {}) as RunResult,
    }
  }

  if (event.type === 'run_failed') {
    return {
      ...state,
      status: 'failed',
      activeNode: undefined,
      error: String(event.payload.message ?? '多智能体流程运行失败。'),
    }
  }

  return state
}
