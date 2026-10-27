import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import EvaluationsView from './EvaluationsView'
import { evaluationRequest, type EvaluationJob, type QuestionBank } from './evaluationApi'

vi.mock('./evaluationApi', () => ({ evaluationRequest: vi.fn() }))

const bank: QuestionBank = { bank_id: 'bank_1', version: 1, name: 'Questions', description: '', count: 2,
  labelled: 2, contextual: 2, sha256: 'bank-hash', preview: [{ _id: 'q1', question: 'Where is Cedar?', answer: 'Northbridge' }] }
const job: EvaluationJob = { job_id: 'eval_1', name: 'Comparison', status: 'draft', error: null, bank,
  context_mode: 'question_context', sample_count: 2, repeats: 1, fingerprint: 'frozen-hash',
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
