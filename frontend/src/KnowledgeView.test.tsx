import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import KnowledgeView from './KnowledgeView'
import { getKnowledgeChunks, getKnowledgeSource, getKnowledgeSources, importKnowledge, KnowledgeImportError } from './api'
import type { KnowledgeSource } from './types'

vi.mock('./api', async (original) => ({
  ...await original<typeof import('./api')>(),
  getKnowledgeChunks: vi.fn(), getKnowledgeSource: vi.fn(), getKnowledgeSources: vi.fn(), importKnowledge: vi.fn(),
}))

const source: KnowledgeSource = {
  source_id: 'user_notes', name: 'English notes', status: 'ready', current_version: 2, file_count: 1, chunk_count: 1,
  versions: [2, 1].map((version) => ({ version, status: 'ready', created_at: 'now', file_count: 1, chunk_count: 1,
    files: [{ filename: 'notes.md', size_bytes: 100, sha256: 'hash' }], error: null })),
}
const chunks = { items: [{ text: 'Mara Vale founded Cedar Institute.', document_id: 'd1', chunk_id: 'c1',
  filename: 'notes.md', heading_path: ['People'], line_start: 3, line_end: 3, char_start: 10, char_end: 42 }],
  total: 1, limit: 10, offset: 0 }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getKnowledgeSources).mockResolvedValue({ items: [source] })
  vi.mocked(getKnowledgeSource).mockResolvedValue(source)
  vi.mocked(getKnowledgeChunks).mockResolvedValue(chunks)
  vi.mocked(importKnowledge).mockResolvedValue(source)
})
afterEach(cleanup)

describe('KnowledgeView', () => {
  it('loads stored materials and allows using an older ready version', async () => {
    const onUse = vi.fn()
    render(<KnowledgeView disabled={false} onChanged={vi.fn()} onUse={onUse} />)
    fireEvent.click(await screen.findByRole('button', { name: /English notes/ }))
    expect(await screen.findByText('Mara Vale founded Cedar Institute.')).toBeInTheDocument()
    expect(screen.getByText('notes.md · 第 3–3 行')).toBeInTheDocument()
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '材料版本' }))
    fireEvent.click(await screen.findByText('版本 1 · 可用', { selector: '.ant-select-item-option-content' }))
    await waitFor(() => expect(getKnowledgeChunks).toHaveBeenLastCalledWith('user_notes', 1, 0, expect.any(AbortSignal)))
    await waitFor(() => expect(screen.getByRole('button', { name: /用于问答/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /用于问答/ }))
    expect(onUse).toHaveBeenCalledWith('user_notes', 1)
  })

  it('uploads chosen files and refreshes runtime options after import', async () => {
    const onChanged = vi.fn().mockResolvedValue(undefined)
    render(<KnowledgeView disabled={false} onChanged={onChanged} onUse={vi.fn()} />)
    fireEvent.change(screen.getByRole('textbox', { name: '知识源名称' }), { target: { value: 'English notes' } })
    const file = new File(['English text'], 'notes.md', { type: 'text/markdown' })
    fireEvent.change(screen.getByLabelText('材料文件'), { target: { files: [file] } })
    fireEvent.click(screen.getByRole('button', { name: /导入材料/ }))
    expect(await screen.findByText('已导入「English notes」版本 2。')).toBeInTheDocument()
    expect(importKnowledge).toHaveBeenCalledWith('English notes', [file], undefined)
    expect(onChanged).toHaveBeenCalledOnce()
  })

  it('shows import failure and prevents use of a failed version', async () => {
    const failed: KnowledgeSource = { ...source, status: 'failed', current_version: null,
      versions: [{ version: 1, status: 'failed', created_at: 'now', error: { code: 'invalid_encoding', message: '请使用 UTF-8 编码' } }] }
    vi.mocked(getKnowledgeSources).mockResolvedValue({ items: [failed] })
    render(<KnowledgeView disabled={false} onChanged={vi.fn()} onUse={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: /English notes/ }))
    expect(screen.getByText('请使用 UTF-8 编码')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /用于问答/ })).toBeDisabled()
    expect(getKnowledgeChunks).not.toHaveBeenCalled()
  })

  it('locks material uploads during an active run', () => {
    render(<KnowledgeView disabled onChanged={vi.fn()} onUse={vi.fn()} />)
    expect(screen.getByLabelText('材料文件')).toBeDisabled()
    expect(screen.getByRole('button', { name: /导入材料/ })).toBeDisabled()
  })

  it('explains a rejected upload and reloads its failed source record', async () => {
    vi.mocked(importKnowledge).mockRejectedValue(new KnowledgeImportError('请使用 UTF-8 编码', 'user_notes', 3))
    const onChanged = vi.fn()
    render(<KnowledgeView disabled={false} onChanged={onChanged} onUse={vi.fn()} />)
    fireEvent.change(screen.getByRole('textbox', { name: '知识源名称' }), { target: { value: 'Bad notes' } })
    fireEvent.change(screen.getByLabelText('材料文件'), { target: { files: [new File(['bad'], 'notes.txt')] } })
    fireEvent.click(screen.getByRole('button', { name: /导入材料/ }))
    expect(await screen.findByText('请使用 UTF-8 编码')).toBeInTheDocument()
    await waitFor(() => expect(getKnowledgeSource).toHaveBeenCalledWith('user_notes'))
    expect(onChanged).not.toHaveBeenCalled()
  })
})
