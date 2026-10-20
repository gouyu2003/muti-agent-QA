import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import AgentResults from './AgentResults'

afterEach(cleanup)

it('distinguishes model output failures and shows actual calls rather than claiming missing evidence', () => {
  render(<AgentResults results={{ a: { agent_name: '专家 A', answer: '', verified: false, failure_type: 'output_error',
    model_diagnostics: { retry_count: 1, output_error: 'truncated_response', calls: [
      { attempt: 1, duration_ms: 1200, output_chars: 20, finish_reason: 'length', completion_tokens: 4096, outcome: 'truncated_response' },
      { attempt: 2, duration_ms: 1000, output_chars: 0, finish_reason: 'length', outcome: 'truncated_response' },
    ] } } }} />)
  fireEvent.click(screen.getByText('专家 A · 未能作答'))
  expect(screen.getByText('模型输出错误')).toBeInTheDocument()
  expect(screen.getByText(/模型调用 2 次 · 输出重试 1 次/)).toBeInTheDocument()
  expect(screen.getAllByText(/输出被截断/)).toHaveLength(2)
  expect(screen.queryByText('证据不足或存在冲突')).not.toBeInTheDocument()
})

it('presents an evidence-backed partial answer and the remaining knowledge gaps', () => {
  render(<AgentResults results={{ b: { agent_name: '专家 B', answer: 'Suzhou', verified: true,
    answer_scope: 'partial', missing_information: ['start date'] } }} />)
  fireEvent.click(screen.getByText('专家 B · Suzhou'))
  expect(screen.getByText('提供部分信息，交由审核者汇总')).toBeInTheDocument()
  expect(screen.getByText('本实例材料未提供：start date')).toBeInTheDocument()
})
