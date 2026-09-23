import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { getRunDetail, getRuns, getRunStats, getSystemInfo } from './api'
import type { RunStatsSummary, StoredRunDetail, StoredRunRecord, SystemInfo } from './types'

vi.mock('./api', () => ({
  getSystemInfo: vi.fn(),
  streamQuestion: vi.fn(),
  getRuns: vi.fn(),
  getRunStats: vi.fn(),
  getRunDetail: vi.fn(),
  getRunsExportUrl: vi.fn(() => '/api/v1/exports/runs.jsonl'),
}))

const archivedRun: StoredRunRecord = {
  context: {
    run_id: 'r-1', entrypoint: 'api', question: 'Who held the office?',
    source_id: 'hotpotqa_fixture', record_id: 'fixture_001',
    workflow_id: 'multi_hop_qa_v0', method_id: 'multi_agent_adaptive_v0',
    model_name: 'deepseek-v4-flash-0731', protocol_id: 'multi_hop_qa_v0',
    code_version: 'test-commit', config_snapshot: {},
  },
  status: 'completed',
  started_at: '2026-09-23T08:00:00+00:00',
  finished_at: '2026-09-23T08:00:02+00:00',
  final_state: {
    answer: 'Chief of Protocol', verified: true, failure_type: 'none',
    supporting_evidence: [{ sq_id: 'sq_1', text: 'Evidence text', source_title: 'Source A' }],
  },
  metrics: {
    duration_ms: 2000, node_call_count: 3, llm_call_count: 2,
    prompt_tokens: 100, completion_tokens: 40, total_tokens: 140,
    estimated_cost_usd: null, plan_retry_count: 0,
    retrieval_retry_count: 0, validation_retry_count: 1,
  },
  error: null,
}

const archivedDetail: StoredRunDetail = {
  run: archivedRun,
  events: [
    {
      run_id: 'r-1', sequence: 0, event_type: 'run_started',
      timestamp: '2026-09-23T08:00:00+00:00', node: null, cycle: null,
      duration_ms: null, payload: { question: 'Who held the office?' },
    },
    {
      run_id: 'r-1', sequence: 1, event_type: 'route_decided',
      timestamp: '2026-09-23T08:00:02+00:00', node: 'answer_validator_v0', cycle: 1,
      duration_ms: null, payload: { target: '__end__', reason: 'verified' },
    },
  ],
}

const archivedStats: RunStatsSummary = {
  total_runs: 1, completed_runs: 1, failed_runs: 0, cancelled_runs: 0,
  running_runs: 0, verified_runs: 1, completion_rate: 1,
  verification_pass_rate: 1, average_duration_ms: 2000,
  average_retry_count: 0, failure_types: {},
}

const systemInfo: SystemInfo = {
  name: 'Multi-Agent QA',
  version: 'v0',
  protocol_id: 'multi_hop_qa_v0',
  model_name: 'deepseek-v4-flash-0731',
  agents: [
    { agent_id: 'planner_v0', name: '规划智能体', role: 'planner', description: '' },
    { agent_id: 'retriever_v0', name: '检索智能体', role: 'retriever', description: '' },
    {
      agent_id: 'answer_validator_v0',
      name: '回答与验证智能体',
      role: 'answer_validator',
      description: '',
    },
  ],
  flow: [],
  retry_limits: { retrieval_retry_count: 3, plan_retry_count: 2 },
  dataset: {
    name: 'HotpotQA',
    record_id: 'fixture',
    question_type: 'bridge',
    level: 'hard',
    context_count: 10,
    supporting_fact_count: 3,
    default_question: 'A multi-hop question?',
  },
}

describe('App', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getSystemInfo).mockResolvedValue(systemInfo)
    vi.mocked(getRuns).mockResolvedValue({ items: [archivedRun], total: 1, limit: 10, offset: 0 })
    vi.mocked(getRunStats).mockResolvedValue(archivedStats)
    vi.mocked(getRunDetail).mockResolvedValue(archivedDetail)
  })

  it('renders the V0 workbench with Ant Design controls and system metadata', async () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: '观察一次真实的多智能体协作' })).toBeInTheDocument()
    expect(await screen.findByText('服务正常')).toBeInTheDocument()
    expect(screen.getByDisplayValue('A multi-hop question?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /开始协同问答/ })).toBeEnabled()
    expect(screen.getByText('hard · bridge · 10 个上下文')).toBeInTheDocument()
  })

  it('shows persisted runs, timeline, evidence and export in the history view', async () => {
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: /运行历史/ }))

    expect(await screen.findByRole('heading', { name: '历史运行' })).toBeInTheDocument()
    expect(await screen.findByText('Chief of Protocol')).toBeInTheDocument()
    expect(screen.getByText(/Evidence text/)).toBeInTheDocument()
    expect(screen.getByText(/结束运行 · verified/)).toBeInTheDocument()
    expect(screen.getAllByText('100.0%')).toHaveLength(2)
    expect(screen.getByRole('link', { name: /导出 JSONL/ })).toHaveAttribute(
      'href', '/api/v1/exports/runs.jsonl',
    )
  })

  it('explains the empty archive without inventing statistics', async () => {
    vi.mocked(getRuns).mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 })
    vi.mocked(getRunStats).mockResolvedValue({
      ...archivedStats,
      total_runs: 0, completed_runs: 0, verified_runs: 0,
      completion_rate: 0, verification_pass_rate: 0,
      average_duration_ms: null,
    })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: /运行历史/ }))

    expect(await screen.findByText('还没有运行记录。先去实时运行发起一次问答。')).toBeInTheDocument()
    expect(screen.getByText('选择一条运行，查看完整轨迹与答案。')).toBeInTheDocument()
    expect(getRunDetail).not.toHaveBeenCalled()
  })
})
