import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
