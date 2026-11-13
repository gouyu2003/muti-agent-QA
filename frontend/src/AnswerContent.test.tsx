import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import AnswerContent from './AnswerContent'

afterEach(cleanup)

it('renders a general reply without presenting it as a verification failure', () => {
  render(<AnswerContent result={{ answer: '你好！', verified: false, verification_applicable: false,
    response_parts: [{ kind: 'general', content: '你好！' }] }} />)
  expect(screen.getByText('通用回答')).toBeInTheDocument()
  expect(screen.getByText('你好！')).toBeInTheDocument()
  expect(screen.queryByText('验证未通过')).not.toBeInTheDocument()
  expect(screen.queryByText('验证通过')).not.toBeInTheDocument()
})

it('keeps citations and the verdict within the knowledge section of a mixed reply', () => {
  render(<AnswerContent showReferences result={{ verified: true, verification_applicable: true,
    response_parts: [{ kind: 'knowledge', content: '30 册' }, { kind: 'general', content: '建议**定期阅读**。' }],
    supporting_evidence: [{ sq_id: 'sq_1', source_title: '借阅规则', text: '研究生可借 30 册' }] }} />)
  const knowledge = screen.getByRole('region', { name: '知识库部分' })
  const general = screen.getByRole('region', { name: '通用部分' })
  expect(within(knowledge).getByText('验证通过')).toBeInTheDocument()
  fireEvent.click(within(knowledge).getByText('查看引用 · 1 条'))
  expect(within(knowledge).getByText('研究生可借 30 册')).toBeInTheDocument()
  expect(within(general).getByText('通用回答')).toBeInTheDocument()
  expect(within(general).queryByText('验证通过')).not.toBeInTheDocument()
  expect(within(general).queryByText('借阅规则')).not.toBeInTheDocument()
})

it.each([['insufficient', '资料不足'], ['conflict', '资料存在冲突']] as const)('explains %s without marking general content as failed', (failure_type, label) => {
  render(<AnswerContent result={{ verified: false, failure_type, verification_applicable: true,
    response_parts: [{ kind: 'knowledge', content: '无法确认。' }, { kind: 'general', content: '一般建议。' }] }} />)
  expect(screen.getByText(label)).toBeInTheDocument()
  expect(screen.getByText('通用回答')).toBeInTheDocument()
})

it('preserves legacy rendering and safely renders Markdown in general replies', () => {
  const { container } = render(<AnswerContent result={{ answer: '```python\nprint(30)\n```\n\n<script>alert(1)</script>',
    verified: true }} />)
  expect(screen.getByText('验证通过')).toBeInTheDocument()
  expect(container.querySelector('pre code')).toHaveTextContent('print(30)')
  expect(container.querySelector('script')).toBeNull()
})

it('presents clarification as a pending question', () => {
  render(<AnswerContent result={{ answer: '哪类读者？', verified: false, verification_applicable: false,
    chat_context: { intent: 'clarification' } }} />)
  expect(screen.getByText('等待补充信息')).toBeInTheDocument()
  expect(screen.queryByText('通用回答')).not.toBeInTheDocument()
})
