import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { BrowserRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { getAgentTemplates, protocolRequest, getKnowledgeChunks, getKnowledgeSources, getManagedSystems, getExperiments, getRunDetail, getRuns, getRunsExportUrl, getRunStats, getSystemInfo, getSystemOptions, streamQuestion } from './api'
import catalog from './protocolCatalog.fixture.json'
import type { KnowledgeSource, ManagedSystem, RunStatsSummary, StoredRunDetail, StoredRunRecord, SystemInfo, SystemOptions } from './types'

vi.mock('./api', () => ({
  protocolRequest: vi.fn(async () => ({ items: [] })),
  getManagedSystems: vi.fn(async () => ({ items: [] })),
  getAgentTemplates: vi.fn(async () => ({ items: [] })),
  getKnowledgeSources: vi.fn(async () => ({ items: [] })),
  getKnowledgeChunks: vi.fn(),
  getSystemInfo: vi.fn(),
  getSystemOptions: vi.fn(),
  streamQuestion: vi.fn(),
  getRuns: vi.fn(),
  getRunStats: vi.fn(),
  getRunDetail: vi.fn(),
  getExperiments: vi.fn(),
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

function renderApp(path = '/run') {
  window.history.replaceState(null, '', path)
  return render(<BrowserRouter><App /></BrowserRouter>)
}

async function navigateHistory(delta: number) {
  await act(async () => {
    const changed = new Promise<void>(resolve => window.addEventListener('popstate', () => resolve(), { once: true }))
    window.history.go(delta)
    await changed
  })
}

describe('App', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(protocolRequest).mockImplementation(async path => (path === '/templates' ? catalog : { items: [] }) as never)
    vi.mocked(getAgentTemplates).mockResolvedValue({ items: [] })
    vi.mocked(getManagedSystems).mockResolvedValue({ items: [] })
    vi.mocked(getKnowledgeSources).mockResolvedValue({ items: [] })
    vi.mocked(getSystemInfo).mockResolvedValue(systemInfo)
    vi.mocked(getSystemOptions).mockResolvedValue(systemOptions)
    vi.mocked(streamQuestion).mockResolvedValue()
    vi.mocked(getRuns).mockResolvedValue({ items: [archivedRun], total: 1, limit: 10, offset: 0 })
    vi.mocked(getRunStats).mockResolvedValue(archivedStats)
    vi.mocked(getRunDetail).mockResolvedValue(archivedDetail)
    vi.mocked(getExperiments).mockResolvedValue([])
  })

  it('renders the V0 workbench with Ant Design controls and system metadata', async () => {
    renderApp()

    expect(screen.getByRole('heading', { name: '观察一次真实的多智能体协作' })).toBeInTheDocument()
    expect(await screen.findByText('服务正常')).toBeInTheDocument()
    expect(screen.getByDisplayValue('A multi-hop question?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /开始协同问答/ })).toBeEnabled()
    expect(screen.getByText('hard · bridge · 10 个上下文 · 3 条支撑事实')).toBeInTheDocument()
  })

  it('redirects the root to the run page without adding a history entry', async () => {
    const length = window.history.length
    renderApp('/')
    await waitFor(() => expect(window.location.pathname).toBe('/run'))
    expect(window.history.length).toBe(length)
    expect(screen.getByRole('link', { name: /实时运行/ })).toHaveAttribute('aria-current', 'page')
  })

  it.each([
    ['/run', '实时运行', '观察一次真实的多智能体协作'],
    ['/history', '运行历史', '每次运行，都有迹可循'],
    ['/knowledge', '知识源', '知识源'],
    ['/systems', '我的系统', '我的系统'],
    ['/protocols', '协作协议', '我的协作协议'],
  ])('opens %s directly with the matching navigation selected', async (path, label, heading) => {
    renderApp(path)
    expect(await screen.findByRole('heading', { name: heading })).toBeVisible()
    expect(screen.getByRole('link', { name: new RegExp(label) })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('navigation').querySelectorAll('[aria-current="page"]')).toHaveLength(1)
    expect(window.location.pathname).toBe(path)
  })

  it('supports back and forward navigation while preserving the entered question', async () => {
    renderApp()
    await screen.findByText('服务正常')
    fireEvent.change(screen.getByRole('textbox', { name: '多跳问答问题' }), { target: { value: 'Keep this question' } })
    fireEvent.click(screen.getByRole('link', { name: /我的系统/ }))
    expect(window.location.pathname).toBe('/systems')
    await navigateHistory(-1)
    expect(window.location.pathname).toBe('/run')
    expect(screen.getByDisplayValue('Keep this question')).toBeVisible()
    await navigateHistory(1)
    expect(window.location.pathname).toBe('/systems')
    expect(screen.getByRole('link', { name: /我的系统/ })).toHaveAttribute('aria-current', 'page')
  })

  it('shows a not-found page for an unknown path and links back to the run page', async () => {
    renderApp('/missing')
    expect(screen.getByRole('heading', { name: '页面不存在' })).toBeVisible()
    expect(screen.getByRole('navigation').querySelector('[aria-current="page"]')).toBeNull()
    fireEvent.click(screen.getByRole('link', { name: '返回实时运行' }))
    expect(window.location.pathname).toBe('/run')
    expect(await screen.findByRole('heading', { name: '观察一次真实的多智能体协作' })).toBeVisible()
  })

  it('restores the system filter from the URL after remounting and synchronizes history changes', async () => {
    const mounted = renderApp('/history?systemId=sys_a&keep=1')
    await waitFor(() => expect(getRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ systemId: 'sys_a' }), 10, 0, expect.any(AbortSignal)))
    mounted.unmount()
    renderApp(window.location.pathname + window.location.search)
    await waitFor(() => expect(getRunStats).toHaveBeenLastCalledWith(
      expect.objectContaining({ systemId: 'sys_a' }), expect.any(AbortSignal)))
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '系统筛选' }))
    fireEvent.click(await screen.findByText('全部系统与临时问答', { selector: '.ant-select-item-option-content' }))
    expect(window.location.search).toBe('?keep=1')
    await waitFor(() => expect(getRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ systemId: undefined }), 10, 0, expect.any(AbortSignal)))
    await navigateHistory(-1)
    expect(window.location.search).toBe('?systemId=sys_a&keep=1')
    await waitFor(() => expect(getRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ systemId: 'sys_a' }), 10, 0, expect.any(AbortSignal)))
    await navigateHistory(1)
    await waitFor(() => expect(getRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ systemId: undefined }), 10, 0, expect.any(AbortSignal)))
  })

  it('navigates from a knowledge source to the run page with the selected version', async () => {
    const source: KnowledgeSource = {
      source_id: 'user_notes', name: 'English notes', status: 'ready', current_version: 1, file_count: 1, chunk_count: 1,
      versions: [{ version: 1, status: 'ready', created_at: 'now', file_count: 1, chunk_count: 1, files: [], error: null }],
    }
    vi.mocked(getKnowledgeSources).mockResolvedValue({ items: [source] })
    vi.mocked(getKnowledgeChunks).mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 })
    vi.mocked(getSystemOptions).mockResolvedValue({ ...systemOptions, sources: [...systemOptions.sources, {
      source_id: 'user_notes', name: source.name, source_type: 'document_corpus', is_demo: false,
      record_id: null, question_type: null, level: null, context_count: null, supporting_fact_count: null,
      example_question: null, version: 1, versions: [1],
    }] })
    renderApp('/knowledge')
    await screen.findByText('服务正常')
    fireEvent.click(await screen.findByRole('button', { name: /English notes/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: '用于问答' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '用于问答' }))
    expect(window.location.pathname).toBe('/run')
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    await waitFor(() => expect(streamQuestion).toHaveBeenCalledWith(
      expect.any(String), expect.any(Function), expect.any(AbortSignal),
      { workflowId: 'multi_hop_qa_v0', sourceId: 'user_notes', sourceVersion: 1 }))
  })

  it('receives completion while another page is open and preserves the stream and result', async () => {
    let emit: Parameters<typeof streamQuestion>[1] = () => undefined
    let finish: () => void = () => undefined
    let signal: AbortSignal | undefined
    vi.mocked(streamQuestion).mockImplementation((_question, onEvent, currentSignal) => {
      emit = onEvent
      signal = currentSignal
      onEvent({ type: 'run_started', run_id: 'route-run', timestamp: 'now', payload: {} })
      return new Promise<void>(resolve => { finish = resolve })
    })
    renderApp()
    await screen.findByText('服务正常')
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    fireEvent.click(screen.getByRole('link', { name: /运行历史/ }))
    await screen.findByText('Chief of Protocol')
    expect(signal?.aborted).toBe(false)
    await act(async () => {
      emit({ type: 'run_completed', run_id: 'route-run', timestamp: 'now', payload: {
        result: { answer: 'Completed while away', verified: true, verification_note: 'Evidence checked' }, duration_ms: 100,
      } })
      finish()
    })
    fireEvent.click(screen.getByRole('link', { name: /实时运行/ }))
    expect(await screen.findByText('Completed while away')).toBeVisible()
    expect(signal?.aborted).toBe(false)
    expect(streamQuestion).toHaveBeenCalledTimes(1)
  })

  it('preserves unsaved system fields when managing protocols and refreshes choices on return', async () => {
    vi.mocked(getAgentTemplates).mockResolvedValue({ items: [{ agent_type: 'planner', name: 'Planner', uses_llm: true, uses_knowledge: false, editable_fields: [], defaults: {
      agent_id: 'plan', agent_type: 'planner', name: 'Planner', description: '', source_id: null, source_version: null, top_k: 3,
      model_config: { model_name: 'test', system_prompt: 'P', temperature: 0, max_tokens: 1024 },
    } }] })
    renderApp()
    fireEvent.click(screen.getByRole('link', { name: /我的系统/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: '新建系统' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '新建系统' }))
    fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: 'Unsaved system' } })
    fireEvent.click(screen.getByRole('button', { name: '创建或管理协作协议' }))
    fireEvent.click(await screen.findByRole('button', { name: '从 单专家问答 创建' }))
    fireEvent.change(screen.getByRole('textbox', { name: '协议名称' }), { target: { value: 'Unsaved protocol' } })
    expect(window.location.pathname).toBe('/protocols')
    const count = vi.mocked(protocolRequest).mock.calls.filter(c => c[0] === '').length
    fireEvent.click(screen.getByRole('button', { name: '返回我的系统' }))
    expect(screen.getByDisplayValue('Unsaved system')).toBeVisible()
    await waitFor(() => expect(vi.mocked(protocolRequest).mock.calls.filter(c => c[0] === '').length).toBeGreaterThan(count))
    expect(screen.getByRole('combobox', { name: '执行协议' }).closest('.ant-select')).toHaveTextContent('选择个人协议')
    fireEvent.click(screen.getByRole('link', { name: '协作协议' }))
    expect(screen.getByDisplayValue('Unsaved protocol')).toBeVisible()
  })

  it('runs a published multi-source system without requiring a global knowledge source', async () => {
    const managed: ManagedSystem = { system_id: 'sys_team', draft_revision: 1, created_at: 'now', updated_at: 'now', versions: [], draft: {
      name: 'Expert team', description: '', workflow_id: 'multi_expert', source_id: null, source_version: null,
      snapshot: { config_sha256: 'a'.repeat(64), schema_version: '2', execution_mode: 'parallel', protocol: { protocol_name: '多专家汇总' },
        agents: { expert_a: { agent_name: 'Geography specialist', role: 'expert', model_config: { model_name: 'model-a', system_prompt: 'A', temperature: 0, max_tokens: 1024 } },
          expert_b: { agent_name: 'History specialist', role: 'expert', model_config: { model_name: 'model-b', system_prompt: 'B', temperature: 0, max_tokens: 1024 } },
          judge: { agent_name: 'Evidence reviewer', role: 'answer_validator', model_config: { model_name: 'model-a', system_prompt: 'Review', temperature: 0, max_tokens: 1024 } } },
        sources: { expert_a: { source_id: 'source_a', name: 'Geography notes', version: 1, sha256: 'a' }, expert_b: { source_id: 'source_b', name: 'History notes', version: 2, sha256: 'b' } },
        workflow_spec: { protocol_id: 'multi_expert', method_id: 'multi_expert_v1', entry_node: 'expert_a', agents: ['expert_a', 'expert_b', 'judge'], edges: [
          { from: 'expert_a', to: 'judge', route_name: 'always' }, { from: 'expert_b', to: 'judge', route_name: 'always' }, { from: 'judge', to: 'END', route_name: 'always' },
        ], max_cycles: {} },
      },
    } }
    managed.versions = [{ ...managed.draft, version: 'v1', draft_revision: 1 }]
    vi.mocked(getManagedSystems).mockResolvedValue({ items: [managed] })
    renderApp()
    await screen.findByText('服务正常')
    fireEvent.click(screen.getByRole('link', { name: /我的系统/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Expert team/ }))
    fireEvent.click(screen.getByRole('button', { name: '查看此系统历史' }))
    expect(window.location.pathname + window.location.search).toBe('/history?systemId=sys_team')
    await waitFor(() => expect(getRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ systemId: 'sys_team' }), 10, 0, expect.any(AbortSignal)))
    fireEvent.click(screen.getByRole('link', { name: /我的系统/ }))
    fireEvent.click(screen.getByRole('button', { name: '使用此版本问答' }))
    expect(window.location.pathname).toBe('/run')
    expect(screen.getByText('多个模型')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: '知识源' })).not.toBeInTheDocument()
    const start = screen.getByRole('button', { name: /开始协同问答/ })
    expect(start).toBeEnabled()
    fireEvent.click(start)
    await waitFor(() => expect(streamQuestion).toHaveBeenCalledWith(expect.any(String), expect.any(Function), expect.any(AbortSignal),
      { systemId: 'sys_team', systemVersion: 'v1', draftRevision: undefined }))
  })

  it('shows persisted runs, timeline, evidence and export in the history view', async () => {
    renderApp()
    fireEvent.click(screen.getByRole('link', { name: /运行历史/ }))

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
    renderApp()
    fireEvent.click(screen.getByRole('link', { name: /运行历史/ }))

    expect(await screen.findByText('还没有运行记录。先去实时运行发起一次问答。')).toBeInTheDocument()
    expect(screen.getByText('选择一条运行，查看完整轨迹与答案。')).toBeInTheDocument()
    expect(getRunDetail).not.toHaveBeenCalled()
  })

  it('browses V0/V1 batch archives, filters a group and opens a failed sample with its metadata', async () => {
    const experiments = [{ experiment_id: 'v0', name: 'Smoke-10 · V0 原始基线' },
      { experiment_id: 'v1', name: 'Smoke-10 · V1 问答修复' }]
    vi.mocked(getExperiments).mockResolvedValue(experiments)
    const runs: StoredRunRecord[] = ['v1', 'v0'].flatMap((experiment) => Array.from({ length: 10 }, (_, index) => ({
      ...archivedRun,
      context: { ...archivedRun.context, run_id: `${experiment}-${index}`, question: `Batch ${experiment}-${index}`,
        entrypoint: 'batch', experiment_id: experiment, sample_id: `sample-${index}`,
        method_id: `multi_agent_adaptive_${experiment}` },
      status: experiment === 'v0' && index === 0 ? 'failed' : 'completed',
      error: experiment === 'v0' && index === 0 ? { error_type: 'execution_error', message: 'Preserved failure' } : null,
    })))
    const selected = (filters: Parameters<typeof getRuns>[0]) => runs.filter((run) =>
      (!filters.entrypoint || filters.entrypoint === run.context.entrypoint)
      && (!filters.experimentId || filters.experimentId === run.context.experiment_id))
    vi.mocked(getRuns).mockImplementation(async (filters, limit, offset) => {
      const items = selected(filters)
      return { items: items.slice(offset, offset + limit), total: items.length, limit, offset }
    })
    vi.mocked(getRunStats).mockImplementation(async (filters) => ({ ...archivedStats, total_runs: selected(filters).length }))
    vi.mocked(getRunDetail).mockImplementation(async (id) => ({
      run: runs.find((run) => run.context.run_id === id)!,
      events: [{ ...archivedDetail.events[0], run_id: id, event_type: id === 'v0-0' ? 'run_failed' : 'run_completed',
        payload: id === 'v0-0' ? { message: 'Preserved failure' } : {} }],
    }))
    renderApp()
    fireEvent.click(screen.getByRole('link', { name: /运行历史/ }))
    await screen.findByText('20 条')
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '入口筛选' }))
    fireEvent.click(await screen.findByText('批量实验', { selector: '.ant-select-item-option-content' }))
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '实验筛选' }))
    fireEvent.click(await screen.findByText('Smoke-10 · V0 原始基线', { selector: '.ant-select-item-option-content' }))
    await screen.findByText('10 条')
    const list = within(screen.getByLabelText('历史运行列表'))
    fireEvent.click(await list.findByRole('button', { name: /Batch v0-0/ }))
    const config = within(await screen.findByLabelText('生效配置'))
    expect(config.getByText('实验').parentElement).toHaveTextContent('Smoke-10 · V0 原始基线')
    expect(config.getByText('样本编号').parentElement).toHaveTextContent('sample-0')
    expect(await screen.findByText('运行失败')).toBeInTheDocument()
    expect(screen.getAllByText('Preserved failure').length).toBeGreaterThan(0)
    expect(getRunDetail).toHaveBeenCalledWith('v0-0', expect.any(AbortSignal), 'v0')
    expect(getRunsExportUrl).toHaveBeenLastCalledWith(expect.objectContaining({ entrypoint: 'batch', experimentId: 'v0' }))
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '入口筛选' }))
    fireEvent.click(await screen.findByText('API / 前端', { selector: '.ant-select-item-option-content' }))
    await waitFor(() => expect(getRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ entrypoint: 'api', experimentId: undefined }), 10, 0, expect.any(AbortSignal)))
    expect(screen.queryByRole('combobox', { name: '实验筛选' })).not.toBeInTheDocument()
  })

  it('shows the default selection and records its IDs in the stream request', async () => {
    renderApp()
    expect(await screen.findByText('服务正常')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    expect(streamQuestion).toHaveBeenCalledWith(
      'A multi-hop question?', expect.any(Function), expect.any(AbortSignal),
      { workflowId: 'multi_hop_qa_v0', sourceId: 'hotpotqa_fixture' },
    )
  })

  it('disables running when configuration choices cannot be loaded', async () => {
    vi.mocked(getSystemOptions).mockRejectedValue(new Error('配置选项加载失败'))
    renderApp()
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
    renderApp()
    await screen.findByText('服务正常')
    fireEvent.click(screen.getByRole('button', { name: /开始协同问答/ }))
    expect((await screen.findAllByText('待验证')).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/句子索引 0|sentence #0/).length).toBeGreaterThan(0)
  })

  it('switches source without overwriting a typed question', async () => {
    renderApp()
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
    renderApp()
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
    renderApp()
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
    renderApp()
    fireEvent.click(screen.getByRole('link', { name: /运行历史/ }))
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
    renderApp()
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
