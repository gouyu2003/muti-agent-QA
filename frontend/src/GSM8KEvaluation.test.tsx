import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ConfigProvider } from 'antd'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import GSM8KEvaluation from './GSM8KEvaluation'
import { evaluationRequest } from './evaluationApi'

vi.mock('./evaluationApi', () => ({ evaluationRequest: vi.fn() }))

function jobFixture(overrides: Record<string, unknown> = {}) {
  return {
    job_id: 'gsm_example', name: 'GSM8K 训练题复现', state: 'completed', error: null, fake: true,
    model: 'deepseek-v4.1-flash-1017', methods: ['single'], selection: 'debug', samples: 1,
    planned_calls: 1, actual_calls: 1,
    counts: { pending: 0, running: 0, completed: 1, failed: 0, interrupted: 0 },
    results: [], calls: [], report: null, profile_sha256: 'hash', budget_cny: '20',
    pricing: { input_per_million_cny: '2', output_per_million_cny: '8', verified_at: '', source: '' },
    ...overrides,
  }
}
function mockJobs(items: unknown[] = [], detail: unknown = items[0]) {
  vi.mocked(evaluationRequest).mockImplementation(async (path, method) => {
    if (path === '/gsm8k/jobs' && method === 'POST') {
      items = [detail, ...items]
      return detail as never
    }
    if (path === '/gsm8k/jobs') return { items } as never
    if (path.startsWith('/gsm8k/jobs/')) return detail as never
    throw new Error(`unexpected ${path}`)
  })
}
function renderPage() {
  return render(<ConfigProvider theme={{ token: { motion: false } }}><GSM8KEvaluation /></ConfigProvider>)
}
async function openCreate() {
  fireEvent.click(screen.getAllByRole('button', { name: '新建实验' })[0])
  return screen.findByRole('button', { name: '创建实验' })
}

beforeEach(() => { window.history.replaceState(null, '', '/') })
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('creates a fake experiment from the drawer and shows the core scores and agent rounds', async () => {
  const result = { qid: 'debug_0001', method: 'single', repeat: 0, status: 'completed', question: 'How much?', gold: '#### 42',
    outputs: ['\\boxed{42}'], scores: { fixed: { correct: 1, prediction: '42' }, author_faithful: { correct: 1 } } }
  const job = jobFixture({ results: [result], calls: [{ task_key: 'single:0:debug_0001', agent: 0, round: 0, attempt: 1, status: 'completed',
    duration_ms: 1, request: { messages: [{ role: 'user', content: 'How much?' }] },
    response: { content: '\\boxed{42}', finish_reason: 'stop', usage: null }, error: null }],
    report: { usage: { prompt_tokens: null, completion_tokens: null }, unknown_requests: 0 } })
  mockJobs([], job)
  renderPage()
  await openCreate()
  fireEvent.click(screen.getByLabelText('使用固定假回答进行离线检查'))
  fireEvent.click(screen.getByRole('button', { name: '创建实验' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/gsm8k/jobs', 'POST', expect.objectContaining({ fake: true, count: 1 })))
  fireEvent.click(await screen.findByRole('button', { name: '方法对比' }))
  expect(await screen.findByText(/固定评分：1\/1/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '逐题结果' }))
  fireEvent.click(await screen.findByRole('button', { name: /debug_0001.*Single/ }))
  expect(screen.getByText('How much?')).toBeInTheDocument()
  expect(screen.getByText(/第 1 轮 · 智能体 1/)).toBeInTheDocument()
})

it('starts the fixed MMLU-Pro smoke list and loads a Debate trace on demand', async () => {
  const result = { qid: '42', method: 'debate', repeat: 0, status: 'completed', question: 'Question\nA. One\nB. Two',
    category: 'math', gold: 'B', outputs: [], scores: { fixed: { correct: 1, prediction: 'B' } },
    round_scores: [{ candidates: ['A', 'A', 'B'], votes: { A: 2, B: 1 }, prediction: 'A', correct: 0 },
      { candidates: ['B', 'B', 'A'], votes: { B: 2, A: 1 }, prediction: 'B', correct: 1 }] }
  const job = jobFixture({ job_id: 'mmlu_example', name: 'MMLU-Pro 5 题调试', methods: ['debate'], selection: 'smoke',
    samples: 5, planned_calls: 30, actual_calls: 30, results: [result], quotas: { math: 1 } })
  const base = '/mmlu-pro/jobs'
  let created = false
  let started = false
  vi.mocked(evaluationRequest).mockImplementation(async (path, method) => {
    if (path === '/mmlu-pro/manifests') return { items: [{ selection: 'smoke', count: 5 }] } as never
    if (path === base && method === 'POST') created = true
    if (path === base && method !== 'POST') return { items: created ? [job] : [] } as never
    if (path.endsWith('/start')) started = true
    if (path.endsWith('/questions/42/debate')) return { items: [{
      task_key: 'debate:0:42', agent: 0, round: 0, attempt: 1, status: 'completed',
      request: { messages: [] }, response: { content: 'FINAL: A' }, error: null,
    }] } as never
    return (started ? job : { ...job, state: 'draft', results: [], actual_calls: 0 }) as never
  })
  render(<ConfigProvider theme={{ token: { motion: false } }}><GSM8KEvaluation benchmark="mmlu-pro" /></ConfigProvider>)
  fireEvent.click(await openCreate())
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith(base, 'POST', expect.objectContaining({ selection: 'smoke', fake: true })))
  fireEvent.click(await screen.findByRole('button', { name: '启动实验' }))
  fireEvent.click(await screen.findByRole('button', { name: '确认启动' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith(base + '/mmlu_example/start', 'POST',
    { allow_model_calls: false, retry_failed: false }))
  fireEvent.click(screen.getByRole('button', { name: '逐题结果' }))
  fireEvent.click(await screen.findByRole('button', { name: /42.*Debate/ }))
  expect(await screen.findByText(/第 1 轮投票.*A 2 票/)).toBeInTheDocument()
  expect(screen.getByText(/第 2 轮投票.*B 2 票/)).toBeInTheDocument()
  expect(await screen.findByText(/第 1 轮 · 智能体 1/)).toBeInTheDocument()
})

it('shows the calibration manifest quotas and defaults its 60-call plan to Single', async () => {
  vi.mocked(evaluationRequest).mockImplementation(async path => {
    if (path === '/mmlu-pro/jobs') return { items: [] } as never
    if (path === '/mmlu-pro/manifests') return { items: [
      { selection: 'smoke', count: 5 },
      { selection: 'calibration-60', count: 60, split: 'test', seed: 20261125,
        quotas: { chemistry: 15, economics: 15, law: 15, philosophy: 15 } },
    ] } as never
    throw new Error(`unexpected ${path}`)
  })
  render(<ConfigProvider theme={{ token: { motion: false } }}><GSM8KEvaluation benchmark="mmlu-pro" /></ConfigProvider>)
  const create = await openCreate()
  await screen.findByText(/预计 75 次正常生成/)
  fireEvent.mouseDown(screen.getByRole('combobox', { name: 'MMLU-Pro 题目来源' }))
  fireEvent.click(await screen.findByText('60 题难度校准', { exact: true }))
  expect(await screen.findByText(/预计 60 次正常生成/)).toHaveTextContent('1 种方法')
  expect(screen.getByText(/化学 15 题 · 经济学 15 题 · 法律 15 题 · 哲学 15 题/)).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: 'Single' })).toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'Debate' })).not.toBeChecked()
  expect(create).toBeEnabled()
  expect(vi.mocked(evaluationRequest).mock.calls.every(([, method]) => !method || method === 'GET')).toBe(true)
})

it('shows completed method tasks against all planned method tasks', async () => {
  const job = jobFixture({ job_id: 'gsm_progress', name: 'GSM8K formal v1', state: 'paused', fake: false,
    model: 'deepseek-v4.1-flash-1017', methods: ['single', 'reflection', 'majority', 'debate'],
    selection: 'formal', samples: 100, planned_calls: 1500, actual_calls: 750,
    counts: { pending: 200, running: 0, completed: 200, failed: 0, interrupted: 0 },
    frontend_observations: [{ event: 'created', sha256: 'frontend-hash', observed_at: '2026-11-11T00:00:00Z' }] })
  mockJobs([job])
  renderPage()
  const detail = within(await screen.findByRole('region', { name: '实验详情' }))
  expect(detail.getByText('完成任务').parentElement).toHaveTextContent('200/400')
  expect(screen.queryByText('200/100')).not.toBeInTheDocument()
  expect(screen.getByText('创建时前端')).toBeInTheDocument()
  expect(screen.getByText(/frontend-hash/)).toBeInTheDocument()
})

it('pairs method results by question and filters wrong answers separately from disagreements and execution failures', async () => {
  function result(qid: string, method: string, prediction: string | null, correct: number, status = 'completed') {
    return { qid, method, repeat: 0, status, question: `Question ${qid}`, gold: 'Explanation\n#### 16',
      outputs: [], scores: { fixed: { prediction, correct } } }
  }
  const job = jobFixture({ job_id: 'gsm_comparison', name: 'Comparison', state: 'paused', fake: true,
    model: 'fake', methods: ['single', 'debate'], selection: 'debug', samples: 5, planned_calls: 35,
    actual_calls: 0, counts: { pending: 1, running: 0, completed: 8, failed: 1, interrupted: 0 },
    results: [result('0001', 'single', '16', 1), result('0002', 'single', '18', 0), result('0003', 'single', '16', 1),
      result('0004', 'single', null, 0, 'failed'), result('0005', 'single', null, 0),
      result('0001', 'debate', '16', 1), result('0002', 'debate', '18', 0), result('0003', 'debate', '17', 0),
      result('0005', 'debate', '16', 1)],
    report: null, profile_sha256: 'hash', budget_cny: '0' })
  mockJobs([job])
  renderPage()
  fireEvent.click(await screen.findByRole('button', { name: '逐题结果' }))
  const table = within(await screen.findByRole('table', { name: /逐题结果/ }))
  const questionIds = () => table.getAllByRole('rowheader').map(header => header.textContent)
  expect(table.getAllByRole('columnheader').map(header => header.textContent)).toEqual(['题号', '标准答案', 'Single', 'Debate'])
  expect(questionIds()).toEqual(['0001', '0002', '0003', '0004', '0005'])
  const incomplete = within(table.getByRole('row', { name: /0004/ }))
  expect(incomplete.getByRole('button', { name: /执行失败/ })).toBeInTheDocument()
  expect(incomplete.getByText('待出结果')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: '有方法答错' }))
  expect(questionIds()).toEqual(['0002', '0003', '0005'])
  fireEvent.click(screen.getByRole('button', { name: '方法间答案不同' }))
  expect(questionIds()).toEqual(['0003'])
  fireEvent.click(table.getByRole('button', { name: /0003.*Debate.*17.*未答对/ }))
  expect(screen.getByText('题目与轮次轨迹 · 0003 · Debate')).toBeInTheDocument()
  expect(screen.getByText('Question 0003')).toBeInTheDocument()
})

it('opens an existing experiment directly without configuring a new one', async () => {
  const job = jobFixture({ state: 'completed', samples: 4, methods: ['single', 'debate'],
    planned_calls: 8, actual_calls: 8, counts: { pending: 0, running: 0, completed: 8, failed: 0, interrupted: 0 } })
  mockJobs([job], job)
  renderPage()
  expect(await screen.findByRole('heading', { name: 'GSM8K 训练题复现' })).toBeInTheDocument()
  expect(screen.queryByLabelText('GSM8K 实验名称')).not.toBeInTheDocument()
  const rail = within(screen.getByRole('complementary', { name: '实验记录' }))
  expect(rail.getByRole('button', { pressed: true })).toHaveTextContent('GSM8K 训练题复现')
  expect(rail.getByText('8/8')).toBeInTheDocument()
  const detail = within(screen.getByRole('region', { name: '实验详情' }))
  expect(detail.getByText('完成任务').parentElement).toHaveTextContent('8/8')
  fireEvent.click(screen.getByRole('button', { name: '方法对比' }))
  expect((await screen.findAllByText('生成记录 0 次')).length).toBe(2)
  expect(window.location.search).toContain('gsm8k_view=compare')
  expect(window.location.search).toContain('gsm8k_job=gsm_example')
})

it('invites creating the first experiment when no records exist', async () => {
  mockJobs([])
  renderPage()
  expect(await screen.findByText('还没有实验记录，先创建一个实验')).toBeInTheDocument()
  await openCreate()
  expect(screen.getByLabelText('GSM8K 实验名称')).toBeInTheDocument()
})

it.each(['gsm8k', 'mmlu-pro'] as const)('archives and confirms deletion from the %s card menu', async benchmark => {
  const path = `/${benchmark}/jobs`
  const record = jobFixture({ name: '待清理的实验' })
  let archived = false
  vi.mocked(evaluationRequest).mockImplementation(async (url, method, body) => {
    if (url === '/mmlu-pro/manifests') return { items: [] } as never
    if (url === path) return { items: archived ? [] : [record] } as never
    if (url === `${path}?include_archived=true`) return { items: [{ ...record, archived }] } as never
    if (url === `${path}/${record.job_id}/archive`) {
      archived = (body as { archived: boolean }).archived
      return { job_id: record.job_id, archived } as never
    }
    if (url === `${path}/${record.job_id}` && method === 'DELETE') {
      return { job_id: record.job_id, deleted: true } as never
    }
    if (url === `${path}/${record.job_id}`) return { ...record, archived } as never
    throw new Error(`unexpected ${url}`)
  })
  render(<ConfigProvider theme={{ token: { motion: false } }}><GSM8KEvaluation benchmark={benchmark} /></ConfigProvider>)
  const menu = () => screen.findByRole('button', { name: '更多实验操作' })
  fireEvent.click(await menu())
  fireEvent.click(await screen.findByRole('menuitem', { name: '归档实验' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: '更多实验操作' })).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('checkbox', { name: '显示已归档实验' }))
  fireEvent.click(await menu())
  fireEvent.click(await screen.findByRole('menuitem', { name: '删除实验' }))
  const dialog = await screen.findByRole('dialog', { name: '永久删除实验？' })
  expect(dialog).toHaveTextContent('待清理的实验')
  expect(evaluationRequest).not.toHaveBeenCalledWith(`${path}/${record.job_id}`, 'DELETE')
  fireEvent.click(within(dialog).getByRole('button', { name: '删除实验' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith(`${path}/${record.job_id}`, 'DELETE'))
  await waitFor(() => expect(screen.queryByRole('button', { name: '更多实验操作' })).not.toBeInTheDocument())
})

it('restores the selected experiment and detail view from the URL', async () => {
  window.history.replaceState(null, '', '/?gsm8k_job=gsm_example&gsm8k_view=results')
  const result = { qid: 'debug_0001', method: 'single', repeat: 0, status: 'completed', question: 'How much?', gold: '#### 42',
    outputs: [], scores: { fixed: { correct: 1, prediction: '42' } } }
  const job = jobFixture({ results: [result] })
  mockJobs([job], job)
  renderPage()
  expect(await screen.findByRole('table', { name: /逐题结果/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /debug_0001.*Single/ })).toBeInTheDocument()
})
