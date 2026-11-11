import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ConfigProvider } from 'antd'
import { afterEach, expect, it, vi } from 'vitest'
import GSM8KEvaluation from './GSM8KEvaluation'
import { evaluationRequest } from './evaluationApi'

vi.mock('./evaluationApi', () => ({ evaluationRequest: vi.fn() }))

afterEach(() => { cleanup(); vi.clearAllMocks() })

it('creates a fake experiment and shows the core scores and agent rounds', async () => {
  const result = { qid: 'debug_0001', method: 'single', repeat: 0, status: 'completed', question: 'How much?', gold: '#### 42',
    outputs: ['\\boxed{42}'], scores: { fixed: { correct: 1, prediction: '42' }, author_faithful: { correct: 1 } } }
  const job = { job_id: 'gsm_example', name: 'GSM8K 训练题复现', state: 'completed', error: null, fake: true,
    model: 'deepseek-v4.1-flash-1017', methods: ['single'], selection: 'debug', samples: 1, planned_calls: 1,
    actual_calls: 1, counts: { pending: 0, running: 0, completed: 1, failed: 0, interrupted: 0 },
    results: [result], calls: [{ task_key: 'single:0:debug_0001', agent: 0, round: 0, attempt: 1, status: 'completed',
      duration_ms: 1, request: { messages: [{ role: 'user', content: 'How much?' }] },
      response: { content: '\\boxed{42}', finish_reason: 'stop', usage: null }, error: null }],
    report: { usage: { prompt_tokens: null, completion_tokens: null }, unknown_requests: 0 },
    profile_sha256: 'hash', budget_cny: '20', pricing: { input_per_million_cny: '2', output_per_million_cny: '8', verified_at: '', source: '' } }
  vi.mocked(evaluationRequest).mockImplementation(async (path, method) => {
    if (path === '/gsm8k/jobs' && method === 'POST') return job as never
    if (path === '/gsm8k/jobs') return { items: [job] } as never
    if (path === '/gsm8k/jobs/gsm_example') return job as never
    throw new Error(`unexpected ${path}`)
  })
  render(<ConfigProvider theme={{ token: { motion: false } }}><GSM8KEvaluation /></ConfigProvider>)
  fireEvent.click(screen.getByLabelText('使用固定假回答进行离线检查'))
  fireEvent.click(screen.getByRole('button', { name: '创建实验' }))
  await waitFor(() => expect(evaluationRequest).toHaveBeenCalledWith('/gsm8k/jobs', 'POST', expect.objectContaining({ fake: true, count: 1 })))
  const resultButton = await screen.findByRole('button', { name: /debug_0001.*Single/ })
  expect(screen.getByText(/固定评分：1\/1/)).toBeInTheDocument()
  fireEvent.click(resultButton)
  expect(screen.getByText('How much?')).toBeInTheDocument()
  expect(screen.getByText(/第 1 轮 · 智能体 1/)).toBeInTheDocument()
})

it('shows completed method tasks against all planned method tasks', async () => {
  const job = { job_id: 'gsm_progress', name: 'GSM8K formal v1', state: 'paused', error: null, fake: false,
    model: 'deepseek-v4.1-flash-1017', methods: ['single', 'reflection', 'majority', 'debate'],
    selection: 'formal', samples: 100, planned_calls: 1500, actual_calls: 750,
    counts: { pending: 200, running: 0, completed: 200, failed: 0, interrupted: 0 },
    results: [], calls: [], report: null, profile_sha256: 'hash', budget_cny: '20',
    frontend_observations: [{ event: 'created', sha256: 'frontend-hash', observed_at: '2026-11-11T00:00:00Z' }],
    pricing: { input_per_million_cny: '2', output_per_million_cny: '8', verified_at: '', source: '' } }
  vi.mocked(evaluationRequest).mockImplementation(async (path, method) => {
    if (path === '/gsm8k/jobs' && method === 'POST') return job as never
    if (path === '/gsm8k/jobs') return { items: [job] } as never
    if (path === '/gsm8k/jobs/gsm_progress') return job as never
    throw new Error(`unexpected ${path}`)
  })
  render(<ConfigProvider theme={{ token: { motion: false } }}><GSM8KEvaluation /></ConfigProvider>)
  fireEvent.click(screen.getByRole('button', { name: '创建实验' }))
  expect(await screen.findByText('200/400')).toBeInTheDocument()
  expect(screen.getByText('完成任务')).toBeInTheDocument()
  expect(screen.queryByText('200/100')).not.toBeInTheDocument()
  expect(screen.getByText('创建时前端')).toBeInTheDocument()
  expect(screen.getByText(/frontend-hash/)).toBeInTheDocument()
})

it('pairs method results by question and filters wrong answers separately from disagreements and execution failures', async () => {
  function result(qid: string, method: string, prediction: string | null, correct: number, status = 'completed') {
    return { qid, method, repeat: 0, status, question: `Question ${qid}`, gold: 'Explanation\n#### 16',
      outputs: [], scores: { fixed: { prediction, correct } } }
  }
  const job = { job_id: 'gsm_comparison', name: 'Comparison', state: 'paused', error: null, fake: true,
    model: 'fake', methods: ['single', 'debate'], selection: 'debug', samples: 5, planned_calls: 35,
    actual_calls: 0, counts: { pending: 1, running: 0, completed: 8, failed: 1, interrupted: 0 },
    results: [result('0001', 'single', '16', 1), result('0002', 'single', '18', 0), result('0003', 'single', '16', 1),
      result('0004', 'single', null, 0, 'failed'), result('0005', 'single', null, 0),
      result('0001', 'debate', '16', 1), result('0002', 'debate', '18', 0), result('0003', 'debate', '17', 0),
      result('0005', 'debate', '16', 1)],
    report: null, profile_sha256: 'hash', budget_cny: '0' }
  vi.mocked(evaluationRequest).mockImplementation(async (path, method) => {
    if (path === '/gsm8k/jobs' && method === 'POST') return job as never
    if (path === '/gsm8k/jobs') return { items: [job] } as never
    if (path === '/gsm8k/jobs/gsm_comparison') return job as never
    throw new Error(`unexpected ${path}`)
  })
  render(<ConfigProvider theme={{ token: { motion: false } }}><GSM8KEvaluation /></ConfigProvider>)
  fireEvent.click(screen.getByRole('button', { name: '创建实验' }))
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
