import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import KnowledgeSearch from './KnowledgeSearch'
import { searchKnowledge } from './api'
vi.mock('./api', () => ({ searchKnowledge: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('searches the fixed material version and shows literal source locations', async () => {
  vi.mocked(searchKnowledge).mockResolvedValue({ query: '材料', source_id: 's', version: 2, index_version: 'bm25-zh-mixed-v1', tokenizer: 'cjk', top_k: 5,
    items: [{ chunk_id: 'c1', text: '会议审批需提交邀请函。', filename: '制度.md', source_title: '制度.md', line_start: 3, line_end: 3, relevance_score: 1.2 }] })
  render(<KnowledgeSearch sourceId="s" version={2} />)
  expect(screen.getByRole('button', { name: '检索' })).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox', { name: '检索问题' }), { target: { value: '材料' } })
  fireEvent.click(screen.getByRole('button', { name: '检索' }))
  expect(await screen.findByText('会议审批需提交邀请函。')).toBeInTheDocument()
  expect(screen.getByText(/制度.md · 第 3–3 行/)).toBeInTheDocument()
  expect(searchKnowledge).toHaveBeenCalledWith('s', 2, '材料', 5, expect.any(AbortSignal))
})
