import type { NodeId, RunResult, RunViewState, StreamEvent } from './types'

export const initialRunState: RunViewState = {
  status: 'idle',
  steps: [],
}

export type RunAction =
  | { type: 'reset' }
  | { type: 'clear' }
  | { type: 'cancel' }
  | { type: 'client_error'; message: string }
  | { type: 'stream_event'; event: StreamEvent }

function isNodeId(value: unknown): value is NodeId {
  return typeof value === 'string' && value.length > 0
}

export function runReducer(state: RunViewState, action: RunAction): RunViewState {
  if (action.type === 'clear') return initialRunState
  if (action.type === 'reset') return { status: 'running', steps: [] }
  if (action.type === 'cancel') {
    return { ...state, status: 'cancelled', activeNode: undefined, error: undefined,
      steps: state.steps.map(s => s.status === 'running' ? { ...s, status: 'cancelled' } : s) }
  }
  if (action.type === 'client_error') {
    return { ...state, status: 'failed', activeNode: undefined, error: action.message,
      steps: state.steps.map(s => s.status === 'running' ? { ...s, status: 'failed' } : s) }
  }

  const { event } = action
  if (event.type === 'run_started') {
    return { ...state, status: 'running', runId: event.run_id }
  }

  if (event.type === 'node_started') {
    const node = event.payload.node
    const cycle = Number(event.payload.cycle ?? 1)
    if (!isNodeId(node)) return state
    const callId = typeof event.payload.call_id === 'string' ? event.payload.call_id : undefined
    if (callId && state.steps.some(s => s.callId === callId)) return state
    return {
      ...state,
      activeNode: node,
      steps: [
        ...state.steps,
        {
          key: callId ?? `${node}-${cycle}-${state.steps.length}`,
          callId,
          stageId: typeof event.payload.stage_id === 'string' ? event.payload.stage_id : undefined,
          taskId: typeof event.payload.task_id === 'string' ? event.payload.task_id : undefined,
          node,
          agentName: typeof event.payload.agent_name === 'string' ? event.payload.agent_name : undefined,
          agentType: typeof event.payload.agent_type === 'string' ? event.payload.agent_type : undefined,
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
    const callId = typeof event.payload.call_id === 'string' ? event.payload.call_id : undefined
    const index = [...state.steps]
      .map((step, itemIndex) => ({ step, itemIndex }))
      .reverse()
      .find(({ step }) => callId ? step.callId === callId : step.node === node && step.status === 'running')?.itemIndex
    const output = (event.payload.output ?? {}) as Partial<RunResult>
    const callFailed = callId && output.protocol_calls?.[callId]?.status === 'failed'
    const steps = [...state.steps]
    if (index === undefined) {
      steps.push({
        key: callId ?? `${node}-${cycle}-${steps.length}`,
        callId,
        stageId: typeof event.payload.stage_id === 'string' ? event.payload.stage_id : undefined,
        taskId: typeof event.payload.task_id === 'string' ? event.payload.task_id : undefined,
        node,
        agentName: typeof event.payload.agent_name === 'string' ? event.payload.agent_name : undefined,
        agentType: typeof event.payload.agent_type === 'string' ? event.payload.agent_type : undefined,
        cycle,
        status: callFailed ? 'failed' : 'completed',
        timestamp: event.timestamp,
        output,
      })
    } else {
      steps[index] = { ...steps[index], status: callFailed ? 'failed' : 'completed', output }
    }
    return { ...state, steps, activeNode: [...steps].reverse().find(step => step.status === 'running')?.node }
  }

  if (event.type === 'route_decided') return { ...state, routes: [...(state.routes ?? []), event.payload] }

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
      steps: state.steps.map(s => s.status === 'running' ? { ...s, status: 'failed' } : s),
    }
  }
  if (event.type === 'run_cancelled') {
    return { ...state, status: 'cancelled', activeNode: undefined,
      steps: state.steps.map(s => s.status === 'running' ? { ...s, status: 'cancelled' } : s) }
  }

  return state
}
