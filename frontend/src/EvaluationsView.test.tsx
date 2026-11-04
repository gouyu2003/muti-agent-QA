import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ConfigProvider } from 'antd'
import EvaluationsView from './EvaluationsView'
import { evaluationRequest, type EvaluationJob, type QuestionBank, type EvaluationManifest } from './evaluationApi'
import EvaluationDataPlan from './EvaluationDataPlan'
import EvaluationResearch from './EvaluationResearch'
import EvaluationReview from './EvaluationReview'
import EvaluationPlans from './EvaluationPlans'
import EvaluationArchives from './EvaluationArchives'
import { diagnosticDetail } from './test/diagnosticFixtures'
import type { StoredRunDetail } from './types'

vi.mock('./evaluationApi', () => ({ evaluationRequest: vi.fn() }))

const bank: QuestionBank = { bank_id: 'bank_1', version: 1, name: 'Questions', description: '', count: 2,
  labelled: 2, contextual: 2, sha256: 'bank-hash', preview: [{ _id: 'q1', question: 'Where is Cedar?', answer: 'Northbridge' }] }
const job: EvaluationJob = { job_id: 'eval_1', name: 'Comparison', status: 'draft', error: null, bank,
  context_mode: 'question_context', sample_count: 2, repeats: 1, fingerprint: 'frozen-hash', retryable_count: 0,
  targets: [{ method_id: 'single_rag', label: 'single_rag' }],
  progress: { attempted: 0, planned: 2, completed: 0, failed: 0, interrupted: 0 }, report: null }

beforeEach(() => {
  vi.mocked(evaluationRequest).mockImplementation(async (path, method) => {
    if (path === '/question-banks') return method === 'POST' ? bank : { items: [bank] }
    if (path === '/options') return { methods: ['single_rag', 'debate'], systems: [{ system_id: 'sys_1', version: 'v2', name: 'Custom QA' }] }
    if (path === '/jobs') return method === 'POST' ? job : { items: [job] }
    if (path === '/preflight') return { token: 'checked-token', planned_tasks: 2, sample_count: 2, model_calls: 0,
      data_sha256: 'data-hash', purpose: 'development', manifest: null, targets: [], request: {} }
    if (path.includes('/results')) return { items: [], total: 0 }
    if (path.endsWith('/start')) return { ...job, status: 'queued' }
    return job
  })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

function renderEvaluation() {
  return render(<ConfigProvider theme={{ token: { motion: false } }}><EvaluationsView /></ConfigProvider>)
}

it('explains preflight field validation errors instead of an opaque HTTP status', async () => {
  const api = await vi.importActual<typeof import('./evaluationApi')>('./evaluationApi')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 422,
    json: async () => ({ detail: [{ loc: ['body', 'limits', 'max_node_calls'], msg: 'Input should be less than or equal to 120' }] }) }))
  try { await expect(api.evaluationRequest('/preflight', 'POST', {})).rejects.toThrow('limits.max_node_calls') }
  finally { vi.unstubAllGlobals() }
})

function mockSelectedJob(selected: EvaluationJob) {
  const original = vi.mocked(evaluationRequest).getMockImplementation()!
  vi.mocked(evaluationRequest).mockImplementation(async (path, method, body) => {
    if (path === '/jobs' && !method) return { items: [selected] }
    if (path === '/jobs/eval_1') return selected
    return original(path, method, body)
  })
}

async function beginExperiment() {
  await screen.findByRole('button', { name: /Comparison/ })
  fireEvent.click(screen.getByRole('button', { name: '新建实验' }))
  await screen.findByRole('textbox', { name: '任务名称' })
}

async function freezeFromObjects() {
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  fireEvent.click(screen.getByRole('button', { name: '校验方案' }))
  const freeze = screen.getByRole('button', { name: '冻结并创建任务' })
  await waitFor(() => expect(freeze).toBeEnabled())
  fireEvent.click(freeze)
}

it.each([{ completed: 2, failed: 0 }, { completed: 1, failed: 1 }])(
  'disables empty retries without offering a paid action when first-attempt counts are %o', async counts => {
    mockSelectedJob({ ...job, status: 'completed',
      progress: { ...job.progress, ...counts, attempted: 2 }, retryable_count: 0 })
    renderEvaluation()
    fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
    const retry = await screen.findByRole('button', { name: '补跑失败／状态不明项' })
    expect(retry).toBeDisabled()
    expect(screen.queryByRole('checkbox', { name: /我确认启动会调用真实模型/ })).not.toBeInTheDocument()
    fireEvent.mouseEnter(retry.parentElement!)
    expect(await screen.findByRole('tooltip')).toHaveTextContent('暂无可补跑项，所有任务均已完成。')
    fireEvent.click(retry)
    expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
  },
)

it('shows the latest retry count and requires consent before retrying', async () => {
  mockSelectedJob({ ...job, status: 'completed', retryable_count: 1,
    progress: { ...job.progress, attempted: 2, completed: 1, failed: 1 } })
  renderEvaluation()
  fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
  const retry = await screen.findByRole('button', { name: '补跑失败／状态不明项（1）' })
  expect(retry).toBeEnabled()
  fireEvent.click(retry)
  expect(screen.getByRole('button', { name: '确认补跑' })).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox', { name: /我确认启动会调用真实模型/ }))
  fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }))
  fireEvent.click(retry)
  expect(screen.getByRole('button', { name: '确认补跑' })).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox', { name: /我确认启动会调用真实模型/ }))
  fireEvent.click(screen.getByRole('button', { name: '确认补跑' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/jobs/eval_1/start', 'POST', {
    allow_model_calls: true, retry_failed: true,
  }))
})

it('requires explicit model confirmation and starts a detached job through the API', async () => {
  renderEvaluation()
  fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
  fireEvent.click(await screen.findByRole('button', { name: '启动评测' }))
  const start = await screen.findByRole('button', { name: '启动后台评测' })
  expect(start).toBeDisabled()
  expect(evaluationRequest).not.toHaveBeenCalledWith('/jobs/eval_1/start', expect.anything(), expect.anything())
  fireEvent.click(screen.getByRole('checkbox', { name: /我确认启动会调用真实模型/ }))
  fireEvent.click(start)
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/jobs/eval_1/start', 'POST', { allow_model_calls: true }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('tab', { name: '冻结方案' }))
  expect(await screen.findByText(/关闭页面后任务继续/)).toBeInTheDocument()
})

it('imports a question bank as a version without starting any job', async () => {
  renderEvaluation()
  await screen.findByRole('button', { name: /Comparison/ })
  fireEvent.click(screen.getByRole('button', { name: '题库与清单' }))
  fireEvent.click(screen.getByRole('button', { name: '导入题库' }))
  fireEvent.change(screen.getByRole('textbox', { name: '问题库名称' }), { target: { value: 'New bank' } })
  fireEvent.change(screen.getByLabelText('问题 JSON 文件'), { target: { files: [new File(['[]'], 'questions.json')] } })
  fireEvent.click(screen.getByRole('button', { name: '导入并保存固定版本' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/question-banks', 'POST', expect.any(FormData)))
  const call = vi.mocked(evaluationRequest).mock.calls.find(([path, method]) => path === '/question-banks' && method === 'POST')!
  expect((call[2] as FormData).get('name')).toBe('New bank')
  expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('freezes a named system version with its own materials before execution', async () => {
  renderEvaluation()
  await beginExperiment()
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  fireEvent.click(screen.getByRole('checkbox', { name: 'Custom QA · v2' }))
  await freezeFromObjects()
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/jobs', 'POST', expect.objectContaining({
    bank_id: 'bank_1', bank_version: 1, context_mode: 'system_sources', model: null,
    targets: [{ kind: 'system', system_id: 'sys_1', version: 'v2' }],
  })))
  expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('compares versions across systems and keeps unavailable releases visible with a reason', async () => {
  const original = vi.mocked(evaluationRequest).getMockImplementation()!
  vi.mocked(evaluationRequest).mockImplementation(async (path, method, body) => {
    if (path === '/options') return { methods: [], systems: [
      { system_id: 'lead', name: 'Lead测试系统', version: 'v2', available: true },
      { system_id: 'lead', name: 'Lead测试系统', version: 'v1', available: true },
      { system_id: 'team', name: '英文双专家汇总测试', version: 'v1', available: true },
      { system_id: 'broken', name: '旧系统', version: 'v1', available: false, unavailable_reason: '绑定的知识源文件缺失。' },
    ] }
    return original(path, method, body)
  })
  renderEvaluation()
  await beginExperiment()
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  expect(screen.getByText('绑定的知识源文件缺失。')).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: '旧系统 · v1' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '下一步' })).toBeDisabled()
  for (const label of ['Lead测试系统 · v2', 'Lead测试系统 · v1', '英文双专家汇总测试 · v1']) {
    fireEvent.click(screen.getByRole('checkbox', { name: label }))
  }
  await freezeFromObjects()
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/jobs', 'POST', expect.objectContaining({
    context_mode: 'system_sources', model: null, targets: [
      { kind: 'system', system_id: 'lead', version: 'v2' },
      { kind: 'system', system_id: 'lead', version: 'v1' },
      { kind: 'system', system_id: 'team', version: 'v1' },
    ],
  })))
  expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

const fixedManifest: EvaluationManifest = { manifest_id: 'manifest_1', name: '冻结评价清单', purpose: 'evaluation',
  bank_id: bank.bank_id, bank_version: 1, bank_sha256: bank.sha256, count: 2, sha256: 'manifest-hash',
  type_counts: { bridge: 2 }, samples: [{ sample_id: 'q1', type: 'bridge' }, { sample_id: 'q2', type: 'bridge' }] }

it('freezes a mixed formal experiment only after reviewing the current preflight', async () => {
  const original = vi.mocked(evaluationRequest).getMockImplementation()!
  vi.mocked(evaluationRequest).mockImplementation(async (path, method, body) => {
    if (path === '/manifests') return { items: [fixedManifest] }
    if (path === '/preflight') return { token: 'checked-token', planned_tasks: 6, sample_count: 2, model_calls: 0,
      data_sha256: 'data-hash', purpose: 'evaluation', manifest: fixedManifest, request: body, reference_method: 'single_rag',
      targets: [{ method_id: 'single_rag', label: 'single_rag', models: [], limits: null, workflow: {} }] }
    return original(path, method, body)
  })
  renderEvaluation()
  await beginExperiment()
  fireEvent.click(screen.getByRole('radio', { name: '正式评价' }))
  expect(screen.getByRole('button', { name: '下一步' })).toBeDisabled()
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '固定题目清单' }))
  fireEvent.click(await screen.findByText('冻结评价清单 · 2 题', { selector: '.ant-select-item-option-content' }))
  expect(screen.getByRole('spinbutton', { name: '评测题数' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  for (const label of ['单智能体 RAG', 'Debate', 'Custom QA · v2']) {
    fireEvent.click(screen.getByRole('checkbox', { name: label }))
  }
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  let freeze = screen.getByRole('button', { name: '冻结并创建任务' })
  let check = screen.getByRole('button', { name: '校验方案' })
  expect(freeze).toBeDisabled()
  fireEvent.click(check)
  await waitFor(() => expect(freeze).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '上一步' }))
  fireEvent.change(screen.getByRole('textbox', { name: '评测模型' }), { target: { value: 'changed-model' } })
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  freeze = screen.getByRole('button', { name: '冻结并创建任务' })
  check = screen.getByRole('button', { name: '校验方案' })
  expect(freeze).toBeDisabled()
  fireEvent.click(check)
  await waitFor(() => expect(freeze).toBeEnabled())
  fireEvent.click(freeze)
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/jobs', 'POST', expect.objectContaining({
    purpose: 'evaluation', manifest_id: 'manifest_1', preflight_token: 'checked-token', sample_size: null,
    model: expect.objectContaining({ name: 'changed-model' }), targets: [
      { kind: 'builtin', method: 'single_rag' }, { kind: 'builtin', method: 'debate' },
      { kind: 'system', system_id: 'sys_1', version: 'v2' },
    ],
  })))
  expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('imports a fixed manifest and registers past development exposure through the page', async () => {
  const changed = vi.fn()
  const original = vi.mocked(evaluationRequest).getMockImplementation()!
  vi.mocked(evaluationRequest).mockImplementation(async (path, method, body) => {
    if (path === '/manifests') return method === 'POST' ? fixedManifest : { items: [] }
    if (path === '/exposures') return { count: 1 }
    return original(path, method, body)
  })
  render(<EvaluationDataPlan bank={bank} purpose="evaluation" onPurpose={vi.fn()} onManifest={changed} />)
  fireEvent.click(screen.getByText('生成清单、划分数据与登记曝光'))
  fireEvent.click(screen.getByRole('tab', { name: '导入清单' }))
  const source = { purpose: 'evaluation', dataset_sha256: bank.sha256, samples: [{ sample_id: 'q1' }] }
  const file = new File([JSON.stringify(source)], 'manifest.json', { type: 'application/json' })
  Object.defineProperty(file, 'text', { value: async () => JSON.stringify(source) })
  fireEvent.change(screen.getByLabelText('导入已有清单 JSON'), { target: { files: [file] } })
  fireEvent.click(screen.getByRole('button', { name: '校验并导入固定清单' }))
  await waitFor(() => expect(changed).toHaveBeenCalledWith(fixedManifest))
  expect(evaluationRequest).toHaveBeenCalledWith('/manifests', 'POST', expect.objectContaining({ source_manifest: source }))
  fireEvent.click(screen.getByRole('tab', { name: '曝光登记' }))
  fireEvent.change(screen.getByRole('textbox', { name: '已曝光题目 ID' }), { target: { value: 'q2' } })
  fireEvent.click(screen.getByRole('button', { name: '登记并从正式评价中排除' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/exposures', 'POST', expect.objectContaining({ sample_ids: ['q2'] })))
})

it('shows research pairs, uncertainty and a complete report export without starting models', () => {
  const score = { planned: 2, attempted: 2, complete: true, statuses: { completed: 2, failed: 0 }, scores: { f1: { value: 0.5, scored: 2, observed_mean: 0.5 } }, usage: {} }
  const reportJob: EvaluationJob = { ...job, targets: [{ method_id: 'a', label: 'System A v1' }, { method_id: 'b', label: 'System B v2' }],
    report: { complete: true, methods: {}, retry_count: 0, research: {
      complete: true, primary_metric: 'f1', reference_method: 'a', bootstrap: { seed: 42, replicates: 2000, unit: 'sample', confidence: 0.95 }, notes: ['只使用首次尝试。'],
      methods: { a: { by_type: { bridge: score }, by_repeat: { '1': score }, repeat_score_std: { f1: null } } },
      pairs: [{ left: 'a', right: 'b', paired_samples: 2, planned_samples: 2, complete: true,
        metrics: { f1: { delta: 0.1, ci95_sample_bootstrap: [0.05, 0.15], left_wins: 0, ties: 0, right_wins: 2 }, sp_f1: null } }],
    } } }
  render(<EvaluationResearch job={reportJob} />)
  expect(screen.getByText('10.00%')).toBeInTheDocument()
  expect(screen.getByText('5.00% ～ 15.00%')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '下载完整实验报告包' })).toHaveAttribute('href', '/api/v1/evaluations/jobs/eval_1/report?format=zip')
  expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('reviews latest attempts and same-question full answers while keeping primary scores separate', async () => {
  const open = vi.fn()
  const first = { run_id: 'first', sample_id: 'q1', method_id: 'single_rag', repeat: 0, attempt: 1,
    question: 'A shared question?', answer: '', status: 'failed', scores: { f1: 0 }, error: null, labels: ['runtime_failed'] }
  const latest = { ...first, run_id: 'latest', attempt: 2, answer: 'Latest complete answer', status: 'completed', scores: { f1: 1 }, labels: [] }
  vi.mocked(evaluationRequest).mockImplementation(async path => {
    if (path.includes('/compare?')) return { question: first.question, gold: 'Gold answer', items: [latest,
      { ...first, run_id: null, method_id: 'pipeline_fixed', attempt: 0, status: 'pending' }] }
    if (path.includes('/monitor')) return { sampled_at: '2026-11-03T10:00:00Z', last_event_at: null,
      current: { method_id: 'single_rag', sample_id: 'q1', repeat: 0, attempt: 2, question: first.question,
        status: 'running', elapsed_seconds: 2, phases: [{ stage_id: 'review', agent_name: 'Expert A', node: 'a' }] }, methods: [] }
    if (path.includes('/results')) return { items: [path.includes('view=latest') ? latest : first], total: 1, labels: ['runtime_failed'] }
    return {}
  })
  render(<EvaluationReview job={{ ...job, status: 'running' }} base="/jobs/eval_1" onOpenDetail={open} />)
  expect(await screen.findByRole('status', { name: '当前运行状态' })).toHaveTextContent('Expert A')
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '尝试范围' }))
  fireEvent.click(await screen.findByText('最新尝试（补充）', { selector: '.ant-select-item-option-content' }))
  expect(await screen.findByText('Latest complete answer')).toBeInTheDocument()
  expect(screen.getByText(/主成绩仍按首次尝试统计/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '同题答案对照' }))
  expect(await screen.findByRole('dialog')).toHaveTextContent('Gold answer')
  expect(screen.getByText('尚无运行记录')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '查看该答案轨迹' }))
  expect(open).toHaveBeenCalledWith(latest)
  expect(vi.mocked(evaluationRequest).mock.calls.every(([, method]) => !method || method === 'GET')).toBe(true)
})

it('loads a saved plan as a new draft and saves configurations without starting tasks', async () => {
  const loaded = vi.fn().mockResolvedValue(undefined)
  const stored = { id: 'plan_1', name: 'Stored plan', created_at: '2026-11-03T10:00:00Z', sha256: 'sha',
    source_job_id: null, request: { name: 'Original conditions', bank_id: 'bank_1', bank_version: 1 } }
  vi.mocked(evaluationRequest).mockImplementation(async (path, method) => {
    if (path === '/plans') return method === 'POST' ? { ...stored, id: 'plan_2', name: 'Saved conditions' } : { items: [stored] }
    return {}
  })
  render(<EvaluationPlans request={stored.request} disabled={false} jobId="eval_1" onLoad={loaded} />)
  fireEvent.click(await screen.findByRole('button', { name: '载入为新实验' }))
  await waitFor(() => expect(loaded).toHaveBeenCalledWith(expect.objectContaining({ name: 'Original conditions 副本' }), 'plan_1'))
  fireEvent.click(screen.getByText('保存或导入方案'))
  fireEvent.click(screen.getByRole('button', { name: '保存当前配置为方案' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/plans', 'POST', expect.objectContaining({ request: stored.request })))
  const savedRow = await screen.findByRole('row', { name: /Saved conditions/ })
  expect(within(savedRow).getByRole('link', { name: '导出方案 JSON' })).toHaveAttribute('href', '/api/v1/evaluations/plans/plan_2/export')
  expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('imports CLI snapshots into a read-only workspace with no execution controls', async () => {
  const open = vi.fn()
  const imported = { archive_id: 'archive_1', source_key: 'historical-study', fingerprint: 'archive-sha', imported_at: '2026-11-03T10:00:00Z',
    view: { ...job, job_id: 'archive_1', name: 'CLI · Historical study', readonly: true, status: 'completed' }, original_report: {}, provenance: {}, config: {} }
  const row = { run_id: 'old_run', sample_id: 'q1', method_id: 'single_rag', repeat: 0, attempt: 1, question: 'Historical question', answer: 'Historical answer', status: 'completed', scores: { f1: 1 }, error: null }
  vi.mocked(evaluationRequest).mockImplementation(async (path, method) => {
    if (path === '/archives/candidates') return { items: [{ key: 'historical-study', name: 'Historical study', available: true, reason: null }] }
    if (path === '/archives') return method === 'POST' ? imported : { items: [] }
    if (path.includes('/results')) return { items: [row], total: 1, labels: [] }
    return imported
  })
  render(<EvaluationArchives onOpenDetail={open} />)
  fireEvent.mouseDown(screen.getByRole('combobox', { name: 'CLI 来源档案' }))
  fireEvent.click(await screen.findByText('Historical study', { selector: '.ant-select-item-option-content' }))
  fireEvent.click(screen.getByRole('button', { name: '导入只读快照' }))
  expect(await screen.findByRole('heading', { name: 'CLI · Historical study · 只读' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('tab', { name: '逐题结果' }))
  fireEvent.click(await screen.findByText('查看诊断与轨迹', { selector: 'button span' }))
  expect(open).toHaveBeenCalledWith(row, '/archives/archive_1')
  expect(screen.queryByRole('button', { name: /启动后台评测|补跑失败|暂停/ })).not.toBeInTheDocument()
  expect(vi.mocked(evaluationRequest).mock.calls.filter(([, method]) => method === 'POST').map(([path]) => path)).toEqual(['/archives'])
})

it('copies a task into new editable conditions while restoring its fixed evaluation manifest', async () => {
  const original = vi.mocked(evaluationRequest).getMockImplementation()!
  vi.mocked(evaluationRequest).mockImplementation(async (path, method, body) => {
    if (path === '/manifests') return { items: [fixedManifest] }
    if (path.endsWith('/copy-plan')) return { request: { name: 'Copied experiment', bank_id: 'bank_1', bank_version: 1,
      purpose: 'evaluation', manifest_id: 'manifest_1', targets: [{ kind: 'builtin', method: 'single_rag' }, { kind: 'system', system_id: 'sys_1', version: 'v2' }],
      context_mode: 'question_context', model: { name: 'restored-model', temperature: 0.2, max_tokens: 2000, output_retries: 1 },
      copied_from_job_id: 'eval_1', preflight_token: null } }
    return original(path, method, body)
  })
  renderEvaluation()
  fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
  fireEvent.click(await screen.findByRole('button', { name: '复制为新实验配置' }))
  await waitFor(() => expect(screen.getByRole('textbox', { name: '任务名称' })).toHaveValue('Copied experiment'))
  expect(screen.getByRole('spinbutton', { name: '评测题数' })).toBeDisabled()
  expect(screen.getByText('冻结评价清单 · 2 题')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  expect(screen.getByRole('textbox', { name: '评测模型' })).toHaveValue('restored-model')
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  expect(screen.getByRole('button', { name: '冻结并创建任务' })).toBeDisabled()
  expect(screen.queryByRole('checkbox', { name: /我确认启动会调用真实模型/ })).not.toBeInTheDocument()
  expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('opens recorded diagnostics using only GET requests and keeps the creation draft', async () => {
  const detail = diagnosticDetail()
  const original = vi.mocked(evaluationRequest).getMockImplementation()!
  vi.mocked(evaluationRequest).mockImplementation(async (path, method, body) => {
    if (path.includes('/results')) return { items: [{ run_id: 'run_1', sample_id: 'q1', method_id: 'debate',
      repeat: 0, attempt: 1, question: detail.run.context.question, answer: '', scores: null,
      status: detail.run.status, error: detail.run.error, metrics: detail.run.metrics }], total: 1 }
    if (path === '/jobs/eval_1/runs/run_1') return detail
    return original(path, method, body)
  })
  renderEvaluation()
  await beginExperiment()
  fireEvent.change(screen.getByRole('textbox', { name: '任务名称' }), { target: { value: 'Unsaved comparison' } })
  fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }))
  fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
  fireEvent.click(screen.getByRole('tab', { name: '逐题结果' }))
  fireEvent.click(await screen.findByText('查看诊断与轨迹', { selector: 'button span' }))
  expect(await screen.findByText('request-blocked-001')).toBeInTheDocument()
  fireEvent.click(screen.getByLabelText(/^close$/i, { selector: 'button' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: '新建实验' }))
  expect(screen.getByRole('textbox', { name: '任务名称' })).toHaveValue('Unsaved comparison')
  expect(vi.mocked(evaluationRequest).mock.calls.every(([, method]) => !method || method === 'GET')).toBe(true)
})

it('does not reopen a closed detail when its request resolves late', async () => {
  let resolveDetail!: (value: StoredRunDetail) => void
  const pending = new Promise<StoredRunDetail>(resolve => { resolveDetail = resolve })
  const original = vi.mocked(evaluationRequest).getMockImplementation()!
  vi.mocked(evaluationRequest).mockImplementation(async (path, method, body) => {
    if (path.includes('/results')) return { items: [{ run_id: 'run_1', sample_id: 'q1', method_id: 'debate', repeat: 0,
      attempt: 1, question: 'Q1', answer: '', scores: null, status: 'failed', error: null }], total: 1 }
    if (path === '/jobs/eval_1/runs/run_1') return pending
    return original(path, method, body)
  })
  render(<ConfigProvider theme={{ token: { motion: false } }}><EvaluationsView /></ConfigProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
  fireEvent.click(screen.getByRole('tab', { name: '逐题结果' }))
  fireEvent.click(await screen.findByText('查看诊断与轨迹', { selector: 'button span' }))
  expect(screen.getByRole('status', { name: '正在加载运行详情' })).toBeInTheDocument()
  fireEvent.click(screen.getByLabelText(/^close$/i, { selector: 'button' }))
  resolveDetail(diagnosticDetail())
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(screen.queryByText('request-blocked-001')).not.toBeInTheDocument()
})

it('reloads failed historical diagnostics from the selected archive', async () => {
  const original = vi.mocked(evaluationRequest).getMockImplementation()!
  const imported = { archive_id: 'archive_1', source_key: 'history', fingerprint: 'archive-hash',
    view: { ...job, job_id: 'archive_1', name: 'CLI history', readonly: true }, config: {}, provenance: {} }
  const row = { run_id: 'old_run', sample_id: 'q1', method_id: 'single_rag', repeat: 0, attempt: 1,
    question: 'Historical question', answer: 'Historical answer', status: 'completed', scores: null, error: null }
  let reads = 0
  vi.mocked(evaluationRequest).mockImplementation(async (path, method, body) => {
    if (path === '/archives') return { items: [{ ...imported, name: 'CLI history', imported_at: '2026-11-04', complete: true }] }
    if (path === '/archives/candidates') return { items: [] }
    if (path === '/archives/archive_1') return imported
    if (path.includes('/archives/archive_1/results')) return { items: [row], total: 1, labels: [] }
    if (path === '/archives/archive_1/runs/old_run') {
      reads += 1
      if (reads === 1) throw new Error('Archive detail unavailable')
      return new Promise(() => {})
    }
    return original(path, method, body)
  })
  renderEvaluation()
  await screen.findByRole('button', { name: /Comparison/ })
  fireEvent.click(screen.getByRole('button', { name: '历史档案' }))
  fireEvent.click(await screen.findByRole('button', { name: '查看历史结果' }))
  fireEvent.click(await screen.findByRole('tab', { name: '逐题结果' }))
  fireEvent.click(await screen.findByText('查看诊断与轨迹', { selector: 'button span' }))
  fireEvent.click(await screen.findByRole('button', { name: '重新加载详情' }))
  await waitFor(() => expect(reads).toBe(2))
  expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path === '/jobs/eval_1/runs/old_run')).toBe(false)
})
