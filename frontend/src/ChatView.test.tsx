import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ChatView from './ChatView'
import { cancelBackgroundRun, followBackgroundRun, getManagedSystems } from './api'
import { createConversation, getChatMessages, getConversation, listConversations, savePendingChat, sendChatMessage } from './chatApi'
import type { ChatMessage, Conversation } from './chatApi'
import type { ManagedSystem } from './types'

vi.mock('./api', () => ({ getManagedSystems: vi.fn(), getRunDetail: vi.fn(), followBackgroundRun: vi.fn(), cancelBackgroundRun: vi.fn() }))
vi.mock('./HistoryView', () => ({ RunDetailPanel: () => <div>运行详情</div> }))
vi.mock('./chatApi', async importOriginal => ({ ...await importOriginal<typeof import('./chatApi')>(),
  listConversations: vi.fn(), getConversation: vi.fn(), getChatMessages: vi.fn(), createConversation: vi.fn(), sendChatMessage: vi.fn(),
  renameConversation: vi.fn(), deleteConversation: vi.fn() }))

const snapshot = { config_sha256: 'fixed', agents: {}, workflow_spec: { protocol_id: 'pipeline', method_id: 'fixed', entry_node: 'planner', agents: ['planner'], edges: [], max_cycles: {} },
  protocol: { protocol_name: '流水线' }, source: { source_id: 'library', source_type: 'document_corpus', name: '图书馆规则', version: 1 } }
const system: ManagedSystem = { system_id: 'system-1', draft_revision: 2,
  draft: { name: '图书馆系统', description: '', workflow_id: 'pipeline', source_id: 'library', source_version: 1, snapshot },
  versions: [{ name: '图书馆系统', description: '', workflow_id: 'pipeline', source_id: 'library', source_version: 1, snapshot, version: 'v1' }], created_at: '', updated_at: '' }
const conversation = (id: string, active: string | null = null): Conversation => ({ conversation_id: id, title: `会话 ${id}`, preview: '借阅咨询', active_run_id: active,
  system_ref: { system_id: 'system-1', name: '图书馆系统', kind: 'published', system_version: 'v1' }, config_snapshot: snapshot,
  created_at: '2026-11-13T10:00:00Z', updated_at: '2026-11-13T10:00:00Z' })
const message = (id: string, content: string): ChatMessage => ({ message_id: id, conversation_id: id, role: 'assistant', content, turn_index: 1,
  sequence: 3, run_id: `run-${id}`, status: 'completed', error: null, context: { intent: 'standalone' }, result: { verified: true }, created_at: '' })
function show(path = '/chat') { return render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/chat/:conversationId?" element={<ChatView />} /></Routes></MemoryRouter>) }

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear()
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [system] })
  vi.mocked(listConversations).mockResolvedValue({ items: [conversation('a'), conversation('b')] })
  vi.mocked(getConversation).mockImplementation(async id => conversation(id))
  vi.mocked(getChatMessages).mockImplementation(async id => ({ items: [message(id, `回答 ${id}`)] }))
  vi.mocked(createConversation).mockResolvedValue(conversation('created'))
  vi.mocked(sendChatMessage).mockResolvedValue({ run_id: 'run-created', conversation_id: 'created' })
  vi.mocked(followBackgroundRun).mockImplementation(async (_id, _callback, signal) => {
    await new Promise<void>((_resolve, reject) => signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))
  })
})
afterEach(cleanup)

describe('continuous chat', () => {
  it('keeps a new chat local and respects IME and Shift+Enter before submitting', async () => {
    show()
    expect(createConversation).not.toHaveBeenCalled()
    fireEvent.mouseDown(await screen.findByRole('combobox', { name: '问答系统' }))
    fireEvent.click(await screen.findByText('图书馆系统'))
    const input = screen.getByRole('textbox', { name: '聊天消息' })
    fireEvent.change(input, { target: { value: '研究生能借多少本书？' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true, keyCode: 229 })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(sendChatMessage).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledTimes(1))
    expect(vi.mocked(createConversation).mock.calls[0][0]).toEqual({ system_id: 'system-1', system_version: 'v1' })
    await screen.findByText('回答 created')
  })

  it('ignores stale history when switching conversations', async () => {
    let resolveA!: (value: { items: ChatMessage[] }) => void
    vi.mocked(getChatMessages).mockImplementation(id => id === 'a' ? new Promise(resolve => { resolveA = resolve }) : Promise.resolve({ items: [message(id, `回答 ${id}`)] }))
    show('/chat/a')
    fireEvent.click(await screen.findByRole('link', { name: /会话 b/ }))
    await screen.findByText('回答 b')
    await act(async () => resolveA({ items: [message('a', '过期回答')] }))
    expect(screen.queryByText('过期回答')).not.toBeInTheDocument()
    expect(screen.getByText('回答 b')).toBeInTheDocument()
  })

  it('reuses the saved request id to recover an unconfirmed submission', async () => {
    savePendingChat({ createId: 'create-saved', requestId: 'request-saved', question: '那本科生呢？', conversationId: 'a', selection: { system_id: 'system-1', system_version: 'v1' } }, 'a')
    show('/chat/a')
    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledWith('a', '那本科生呢？', 'request-saved'))
    expect(createConversation).not.toHaveBeenCalled()
  })

  it('keeps the final answer when an older refresh for the same conversation returns late', async () => {
    vi.mocked(getConversation).mockResolvedValue(conversation('a', 'run-a'))
    show('/chat/a')
    await screen.findByText('回答 a')
    const receive = vi.mocked(followBackgroundRun).mock.calls[0][1]
    let resolveOld!: (value: { items: ChatMessage[] }) => void
    vi.mocked(getChatMessages).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    fireEvent(window, new Event('focus'))
    await waitFor(() => expect(resolveOld).toBeDefined())
    vi.mocked(getConversation).mockResolvedValue(conversation('a'))
    vi.mocked(getChatMessages).mockResolvedValue({ items: [message('a', '最终回答')] })
    await act(async () => receive({ type: 'run_completed', run_id: 'run-a', timestamp: '', payload: {} }))
    await screen.findByText('最终回答')
    await act(async () => resolveOld({ items: [message('a', '过期回答')] }))
    expect(screen.queryByText('过期回答')).not.toBeInTheDocument()
    expect(screen.getByText('最终回答')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '停止回答' })).not.toBeInTheDocument()
  })

  it('stops only on explicit action and aborts only the subscription on navigation', async () => {
    vi.mocked(getConversation).mockImplementation(async id => conversation(id, id === 'a' ? 'run-a' : null))
    vi.mocked(cancelBackgroundRun).mockResolvedValue({} as Awaited<ReturnType<typeof cancelBackgroundRun>>)
    show('/chat/a')
    fireEvent.click(await screen.findByRole('button', { name: '停止回答' }))
    expect(cancelBackgroundRun).toHaveBeenCalledWith('run-a')
    const signal = vi.mocked(followBackgroundRun).mock.calls[0][2]
    fireEvent.click(await screen.findByRole('link', { name: /会话 b/ }))
    await screen.findByText('回答 b')
    expect(signal?.aborted).toBe(true)
    expect(cancelBackgroundRun).toHaveBeenCalledTimes(1)
  })

  it('renders Markdown safely with formatted code and a run entry', async () => {
    vi.mocked(getChatMessages).mockResolvedValue({ items: [message('a', '**借阅规则**\n\n```python\nprint(30)\n```\n\n<script>alert(1)</script>')] })
    const { container } = show('/chat/a')
    await screen.findByText('借阅规则')
    expect(container.querySelector('strong')?.textContent).toBeTruthy()
    expect(container.querySelector('pre code')).toHaveTextContent('print(30)')
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByRole('button', { name: '查看本轮运行' })).toBeInTheDocument()
  })
})
