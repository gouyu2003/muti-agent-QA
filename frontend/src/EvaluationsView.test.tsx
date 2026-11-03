import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ConfigProvider } from 'antd'
import EvaluationsView from './EvaluationsView'
import { evaluationRequest, type EvaluationJob, type QuestionBank } from './evaluationApi'
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
    if (path.includes('/results')) return { items: [], total: 0 }
    if (path.endsWith('/start')) return { ...job, status: 'queued' }
    return job
  })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

function mockSelectedJob(selected: EvaluationJob) {
  const original = vi.mocked(evaluationRequest).getMockImplementation()!
  vi.mocked(evaluationRequest).mockImplementation(async (path, method, body) => {
    if (path === '/jobs' && !method) return { items: [selected] }
    if (path === '/jobs/eval_1') return selected
    return original(path, method, body)
  })
}

it.each([{ completed: 2, failed: 0 }, { completed: 1, failed: 1 }])(
  'disables empty retries even after consent when first-attempt counts are %o', async counts => {
    mockSelectedJob({ ...job, status: 'completed',
      progress: { ...job.progress, ...counts, attempted: 2 }, retryable_count: 0 })
    render(<EvaluationsView />)
    fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
    const retry = await screen.findByRole('button', { name: '补跑失败／状态不明项' })
    expect(retry).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /我确认启动会调用真实模型/ }))
    expect(retry).toBeDisabled()
    fireEvent.mouseEnter(retry.parentElement!)
    expect(await screen.findByRole('tooltip')).toHaveTextContent('暂无可补跑项，所有任务均已完成。')
    fireEvent.click(retry)
    expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
  },
)

it('shows the latest retry count and requires consent before retrying', async () => {
  mockSelectedJob({ ...job, status: 'completed', retryable_count: 1,
    progress: { ...job.progress, attempted: 2, completed: 1, failed: 1 } })
  render(<EvaluationsView />)
  fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
  const retry = await screen.findByRole('button', { name: '补跑失败／状态不明项（1）' })
  expect(retry).toBeDisabled()
  fireEvent.focus(retry.parentElement!)
  expect(await screen.findByRole('tooltip')).toHaveTextContent('请先确认模型调用及费用。')
  fireEvent.blur(retry.parentElement!)
  fireEvent.click(screen.getByRole('checkbox', { name: /我确认启动会调用真实模型/ }))
  expect(retry).toBeEnabled()
  fireEvent.click(retry)
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/jobs/eval_1/start', 'POST', {
    allow_model_calls: true, retry_failed: true,
  }))
})

it('requires explicit model confirmation and starts a detached job through the API', async () => {
  render(<EvaluationsView />)
  fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
  const start = await screen.findByRole('button', { name: '启动后台评测' })
  expect(start).toBeDisabled()
  expect(evaluationRequest).not.toHaveBeenCalledWith('/jobs/eval_1/start', expect.anything(), expect.anything())
  fireEvent.click(screen.getByRole('checkbox', { name: /我确认启动会调用真实模型/ }))
  fireEvent.click(start)
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/jobs/eval_1/start', 'POST', { allow_model_calls: true }))
  expect(await screen.findByText(/关闭页面后任务继续/)).toBeInTheDocument()
})

it('imports a question bank as a version without starting any job', async () => {
  render(<EvaluationsView />)
  await screen.findByRole('button', { name: /Comparison/ })
  fireEvent.change(screen.getByRole('textbox', { name: '问题库名称' }), { target: { value: 'New bank' } })
  fireEvent.change(screen.getByLabelText('问题 JSON 文件'), { target: { files: [new File(['[]'], 'questions.json')] } })
  fireEvent.click(screen.getByRole('button', { name: '导入并保存固定版本' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/question-banks', 'POST', expect.any(FormData)))
  const call = vi.mocked(evaluationRequest).mock.calls.find(([path, method]) => path === '/question-banks' && method === 'POST')!
  expect((call[2] as FormData).get('name')).toBe('New bank')
  expect(vi.mocked(evaluationRequest).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('freezes a named system version with its own materials before execution', async () => {
  render(<EvaluationsView />)
  await screen.findByRole('button', { name: /Comparison/ })
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '问题库版本' }))
  fireEvent.click(await screen.findByText('Questions · v1 · 2 题', { selector: '.ant-select-item-option-content' }))
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '评测对象' }))
  fireEvent.click(await screen.findByText('我的系统：已发布版本', { selector: '.ant-select-item-option-content' }))
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '待比较方法' }))
  fireEvent.click(await screen.findByText('Custom QA · v2', { selector: '.ant-select-item-option-content' }))
  fireEvent.click(screen.getByRole('button', { name: '冻结并创建任务（不调用模型）' }))
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
  render(<EvaluationsView />)
  await screen.findByRole('button', { name: /Comparison/ })
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '问题库版本' }))
  fireEvent.click(await screen.findByText('Questions · v1 · 2 题', { selector: '.ant-select-item-option-content' }))
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '评测对象' }))
  fireEvent.click(await screen.findByText('我的系统：已发布版本', { selector: '.ant-select-item-option-content' }))
  expect(screen.getByText('旧系统 · v1：绑定的知识源文件缺失。')).toBeInTheDocument()
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '待比较方法' }))
  fireEvent.click(await screen.findByText('旧系统 · v1（暂不可用）', { selector: '.ant-select-item-option-content' }))
  expect(screen.getByRole('button', { name: '冻结并创建任务（不调用模型）' })).toBeDisabled()
  for (const label of ['Lead测试系统 · v2', 'Lead测试系统 · v1', '英文双专家汇总测试 · v1']) {
    fireEvent.click(await screen.findByText(label, { selector: '.ant-select-item-option-content' }))
  }
  fireEvent.click(screen.getByRole('button', { name: '冻结并创建任务（不调用模型）' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/jobs', 'POST', expect.objectContaining({
    context_mode: 'system_sources', model: null, targets: [
      { kind: 'system', system_id: 'lead', version: 'v2' },
      { kind: 'system', system_id: 'lead', version: 'v1' },
      { kind: 'system', system_id: 'team', version: 'v1' },
    ],
  })))
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
  render(<EvaluationsView />)
  fireEvent.change(screen.getByRole('textbox', { name: '任务名称' }), { target: { value: 'Unsaved comparison' } })
  fireEvent.click(await screen.findByRole('button', { name: /Comparison/ }))
  fireEvent.click(await screen.findByRole('button', { name: '查看诊断与轨迹' }))
  expect(await screen.findByText('request-blocked-001')).toBeInTheDocument()
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
  fireEvent.click(await screen.findByRole('button', { name: '查看诊断与轨迹' }))
  expect(screen.getByRole('status', { name: '正在加载运行详情' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /close/i }))
  resolveDetail(diagnosticDetail())
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(screen.queryByText('request-blocked-001')).not.toBeInTheDocument()
})
