import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { getRunDetail, getRuns, getRunStats, getSystemInfo, getSystemOptions, streamQuestion } from './api'
import type { RunStatsSummary, StoredRunDetail, StoredRunRecord, SystemInfo, SystemOptions } from './types'

vi.mock('./api', () => ({
  getSystemInfo: vi.fn(),
  getSystemOptions: vi.fn(),
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
    code_version: 'test-commit', config_snapshot: {
      system_version: '0.2.0', config_sha256: 'a'.repeat(64),
      source: { sha256: 'b'.repeat(64) },
    },
  },
  status: 'completed',
  started_at: '2026-09-23T08:00:00+00:00',
  finished_at: '2026-09-23T08:00:02+00:00',
  final_state: {
    answer: 'Chief of Protocol', verified: true, failure_type: 'none',
    supporting_evidence: [{ sq_id: 'sq_1', text: 'Evidence text', source_title: 'Source A', source_id: 'hotpotqa_fixture' }],
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

const systemOptions: SystemOptions = {
  system_id: 'multi_agent_qa_demo', system_version: '0.2.0',
  default_workflow_id: 'multi_hop_qa_v0', default_source_id: 'hotpotqa_fixture',
  workflows: [
    { workflow_id: 'multi_hop_qa_v0', name: '反馈重试流程', description: '可补检索与重新规划',
      method_id: 'multi_agent_adaptive_v0', entry_node: 'planner_v0',
      agents: ['planner_v0', 'retriever_v0', 'answer_validator_v0'],
      edges: [
        { source: 'planner_v0', target: 'retriever_v0', route_name: 'always' },
        { source: 'answer_validator_v0', target: 'retriever_v0', route_name: 'insufficient_retry' },
      ], retry_limits: { retrieval_retry_count: 3 } },
    { workflow_id: 'multi_hop_qa_fixed', name: '固定三节点流程', description: '',
      method_id: 'multi_agent_fixed', entry_node: 'planner_v0',
      agents: ['planner_v0', 'retriever_v0', 'answer_validator_v0'],
      edges: [{ source: 'answer_validator_v0', target: 'END', route_name: 'always' }], retry_limits: {} },
  ],
  sources: [
    { source_id: 'hotpotqa_fixture', name: 'HotpotQA', source_type: 'hotpotqa_record',
      is_demo: false, record_id: 'fixture_001', question_type: 'bridge', level: 'hard',
      context_count: 10, supporting_fact_count: 3, example_question: 'A multi-hop question?' },
    { source_id: 'demo_fixture_002', name: '演示语料', source_type: 'hotpotqa_record',
      is_demo: true, record_id: 'demo_fixture_002', question_type: 'bridge', level: 'easy',
      context_count: 2, supporting_fact_count: 2, example_question: 'Demo question?' },
  ],
}

describe('App', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getSystemInfo).mockResolvedValue(systemInfo)
    vi.mocked(getSystemOptions).mockResolvedValue(systemOptions)
    vi.mocked(streamQuestion).mockResolvedValue()
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
    expect(screen.getByText('hard · bridge · 10 个上下文 · 3 条支撑事实')).toBeInTheDocument()
  })

  it('shows persisted runs, timeline, evidence and export in the history view', async () => {
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: /运行历史/ }))

    expect(await screen.findByRole('heading', { name: '历史运行' })).toBeInTheDocument()
    expect(await screen.findByText('Chief of Protocol')).toBeInTheDocument()
    expect(screen.getByText('0.2.0')).toBeInTheDocument()
    expect(screen.getByText('test-commit')).toBeInTheDocument()
    expect(screen.getByText('a'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText('b'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText('fixture_001')).toBeInTheDocument()
    const configuration = within(screen.getByLabelText('生效配置'))
    expect(configuration.getByText('知识源').parentElement).toHaveTextContent('hotpotqa_fixture')
    expect(configuration.getByText('知识源 ID').parentElement).toHaveTextContent('hotpotqa_fixture')
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

  it('shows the default selection and records its IDs in the stream request', async () => {
    render(<App />)
    expect(await screen.findByText('服务正常')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    expect(streamQuestion).toHaveBeenCalledWith(
      'A multi-hop question?', expect.any(Function), expect.any(AbortSignal),
      { workflowId: 'multi_hop_qa_v0', sourceId: 'hotpotqa_fixture' },
    )
  })

  it('disables running when configuration choices cannot be loaded', async () => {
    vi.mocked(getSystemOptions).mockRejectedValue(new Error('配置选项加载失败'))
    render(<App />)
    expect(await screen.findByText('配置不可用')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /开始协同问答/ })).toBeDisabled()
  })

  it('labels retrieved candidates as awaiting verification and preserves sentence zero', async () => {
    vi.mocked(streamQuestion).mockImplementation(async (_question, onEvent) => {
      onEvent({ type: 'run_completed', run_id: 'v1-run', timestamp: 'now', payload: {
        result: { answer: 'Northbridge', verified: true,
          evidence_chunks: [{ sq_id: 'sq_1', sufficiency: 'unknown', insufficiency_note: '',
            chunks: [{ source_title: 'Cedar', source_paragraph: 0, text: 'Cedar is in Northbridge.', relevance_score: 2.4 }] }],
          supporting_evidence: [{ sq_id: 'sq_1', source_title: 'Cedar', source_paragraph: 0, text: 'Cedar is in Northbridge.' }],
        },
      } })
    })
    render(<App />)
    await screen.findByText('服务正常')
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    expect((await screen.findAllByText('待验证')).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/句子索引 0|sentence #0/).length).toBeGreaterThan(0)
  })

  it('switches source without overwriting a typed question', async () => {
    render(<App />)
    await screen.findByText('服务正常')
    fireEvent.change(screen.getByRole('textbox', { name: '多跳问答问题' }), { target: { value: 'My own question?' } })
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '知识源' }))
    fireEvent.click(await screen.findByText('演示语料（演示）', { selector: '.ant-select-item-option-content' }))

    expect(screen.getByDisplayValue('My own question?')).toBeInTheDocument()
    expect(screen.getByText('演示材料')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    await waitFor(() => expect(streamQuestion).toHaveBeenCalledWith(
      'My own question?', expect.any(Function), expect.any(AbortSignal),
      { workflowId: 'multi_hop_qa_v0', sourceId: 'demo_fixture_002' },
    ))
  })

  it('switches to the fixed workflow without showing feedback routes', async () => {
    render(<App />)
    await screen.findByText('服务正常')
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '协作流程' }))
    fireEvent.click(await screen.findByText('固定三节点流程', { selector: '.ant-select-item-option-content' }))
    expect(screen.getByText('固定顺序执行一次，验证后结束。')).toBeInTheDocument()
    expect(screen.queryByText('返回检索')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    await waitFor(() => expect(streamQuestion).toHaveBeenCalledWith(
      'A multi-hop question?', expect.any(Function), expect.any(AbortSignal),
      { workflowId: 'multi_hop_qa_fixed', sourceId: 'hotpotqa_fixture' },
    ))
  })

  it('locks configuration selectors while the selected run is active', async () => {
    let finish: (() => void) | undefined
    vi.mocked(streamQuestion).mockImplementation((_question, onEvent) => {
      onEvent({ type: 'run_started', run_id: 'run-active', timestamp: 'now', payload: {} })
      return new Promise<void>((resolve) => { finish = resolve })
    })
    render(<App />)
    await screen.findByText('服务正常')
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    expect(await screen.findByText(/本次运行：反馈重试流程/)).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: '协作流程' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: '知识源' })).toBeDisabled()
    finish?.()
  })

  it('uses the archived source name for document details and evidence instead of current options', async () => {
    const run: StoredRunRecord = {
      ...archivedRun,
      context: {
        ...archivedRun.context, source_id: 'user_notes', record_id: null,
        config_snapshot: { source: { source_id: 'user_notes', name: '阶段3测试—Aurora Institute', version: 1 } },
      },
      final_state: {
        ...archivedRun.final_state,
        supporting_evidence: [{ sq_id: 'sq_1', source_id: 'user_notes', source_version: 1,
          source_title: 'notes.md', filename: 'notes.md', chunk_id: 'chunk-1',
          line_start: 8, line_end: 10, text: 'Elena Brooks founded Aurora Institute.' }],
      },
    }
    vi.mocked(getRuns).mockResolvedValue({ items: [run], total: 1, limit: 10, offset: 0 })
    vi.mocked(getRunDetail).mockResolvedValue({ ...archivedDetail, run })
    vi.mocked(getSystemOptions).mockResolvedValue({ ...systemOptions, sources: [...systemOptions.sources, {
      source_id: 'user_notes', name: 'Renamed current source', source_type: 'document_corpus', is_demo: false,
      record_id: null, question_type: null, level: null, context_count: null,
      supporting_fact_count: null, example_question: null,
    }] })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: /运行历史/ }))
    await screen.findByText('Elena Brooks founded Aurora Institute.')

    const configuration = within(screen.getByLabelText('生效配置'))
    expect(configuration.getByText('知识源').parentElement).toHaveTextContent('阶段3测试—Aurora Institute')
    expect(configuration.getByText('知识源 ID').parentElement).toHaveTextContent('user_notes')
    expect(configuration.getByText('知识源版本').parentElement).toHaveTextContent('1')
    const evidence = within(screen.getByLabelText('运行证据'))
    expect(evidence.getByTitle('知识源 ID：user_notes')).toHaveTextContent('阶段3测试—Aurora Institute')
    expect(evidence.getByText(/第 8–10 行/)).toHaveTextContent('版本 1 · notes.md')
    expect(screen.queryByText('Renamed current source')).not.toBeInTheDocument()
  })

  it('sends an explicitly selected document version and shows document counts', async () => {
    vi.mocked(getSystemOptions).mockResolvedValue({ ...systemOptions, sources: [...systemOptions.sources, {
      source_id: 'user_notes', name: 'User notes', source_type: 'document_corpus', is_demo: false,
      record_id: null, question_type: null, level: null, context_count: null, supporting_fact_count: null,
      example_question: null, version: 2, versions: [2, 1], file_count: 2, chunk_count: 4,
      version_details: [{ version: 1, file_count: 1, chunk_count: 3 }, { version: 2, file_count: 2, chunk_count: 4 }],
    }] })
    vi.mocked(streamQuestion).mockImplementation(async (_question, onEvent) => {
      onEvent({ type: 'run_completed', run_id: 'doc-run', timestamp: 'now', payload: {
        duration_ms: 1000, result: { answer: 'Document answer', verified: true,
          supporting_evidence: [{ sq_id: 'sq_1', source_id: 'user_notes', source_version: 1,
            filename: 'notes.md', source_title: 'notes.md', chunk_id: 'doc-chunk',
            line_start: 1, line_end: 2, text: 'Original document evidence.' }],
        },
      } })
    })
    render(<App />)
    await screen.findByText('服务正常')
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '知识源' }))
    fireEvent.click(await screen.findByText('User notes', { selector: '.ant-select-item-option-content' }))
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '知识源版本' }))
    fireEvent.click(await screen.findByText('版本 1', { selector: '.ant-select-item-option-content' }))
    expect(screen.getByText('版本 1 · 1 个文件 · 3 个片段')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /填入示例问题/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    expect(streamQuestion).toHaveBeenCalledWith('A multi-hop question?', expect.any(Function), expect.any(AbortSignal),
      { workflowId: 'multi_hop_qa_v0', sourceId: 'user_notes', sourceVersion: 1 })
    expect(await screen.findByTitle('知识源 ID：user_notes')).toHaveTextContent('User notes')
    expect(screen.getByText(/第 1–2 行/)).toHaveTextContent('版本 1 · notes.md')
  })
})
