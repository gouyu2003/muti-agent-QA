import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import ProtocolsView from './ProtocolsView'
import ProtocolBinding from './ProtocolBinding'
import ProtocolCalls from './ProtocolCalls'
import { protocolRequest } from './api'
import type { ManagedProtocol, ProtocolSpec } from './protocolTypes'
import type { ProtocolCall } from './protocolTypes'
import catalog from './protocolCatalog.fixture.json'
import { togglePlanner } from './protocolEditing'
vi.mock('./api', () => ({ protocolRequest: vi.fn() }))
const spec: ProtocolSpec = { language_version: '1', family: 'pipeline', slots: { planner: { role: 'planner', minimum: 1, maximum: 1 } },
  entry_stage: 'plan', stages: [{ stage_id: 'plan', kind: 'plan', slot: 'planner', mode: 'sequential', reads: [], routes: [{ condition: 'always', target: 'END' }], max_visits: 1, on_limit: 'END', instructions: '', share_evidence: true }],
  limits: { max_node_calls: 60, max_llm_calls: 80, max_reserved_tokens: 1000000, timeout_seconds: 600, node_timeout_seconds: 120 } }
beforeEach(() => { vi.resetAllMocks() })
afterEach(cleanup)
test('creates, edits, saves and publishes a user protocol, keeping dirty edits unpublished', async () => {
  const item: ManagedProtocol = { protocol_id: 'p1', draft_revision: 1, archived: false, versions: [], draft: { name: 'My pipeline', description: '', spec, revision: 1, protocol_id: 'p1', spec_sha256: 'hash' } }
  vi.mocked(protocolRequest).mockImplementation(async (path, method) => {
    if (path === '/templates') return catalog as never
    if (path === '') return (method === 'POST' ? item : { items: [] }) as never
    if (path === '/p1/versions') {
      const published = { ...item.draft, version: 'v1' }
      item.versions = [published]
      return published as never
    }
    return item as never
  })
  render(<ProtocolsView disabled={false} />)
  fireEvent.click(await screen.findByText('从 固定流水线 创建'))
  fireEvent.change(screen.getByLabelText('协议名称'), { target: { value: 'My pipeline' } })
  expect(screen.getByRole('button', { name: '发布协议版本' })).toBeDisabled()
  fireEvent.click(screen.getByText('保存协议草稿'))
  await screen.findByText('已保存 r1。')
  expect(screen.getByText('暂无已发布版本')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '发布协议版本' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: '发布协议版本' }))
  await screen.findByText('已发布 v1，可在我的系统绑定。')
  expect(screen.getByRole('combobox', { name: '协议发布版本' }).closest('.ant-select')).toHaveTextContent('v1')
  expect(screen.getByText('My pipeline · v1 · hash')).toBeInTheDocument()
  await waitFor(() => expect(protocolRequest).toHaveBeenCalledWith('/p1/versions', 'POST', { expected_revision: 1 }))
  fireEvent.change(screen.getByLabelText('协议名称'), { target: { value: 'Changed' } })
  expect(screen.getByRole('button', { name: '发布协议版本' })).toBeDisabled()
})

test('offers all six templates and edits optional planning through stages, slots and reads', async () => {
  vi.mocked(protocolRequest).mockImplementation(async path => (path === '/templates' ? catalog : { items: [] }) as never)
  render(<ProtocolsView disabled={false} />)
  for (const template of catalog.items) expect(await screen.findByRole('button', { name: `从 ${template.name} 创建` })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: '从 多专家汇总 创建' }))
  fireEvent.click(screen.getByRole('checkbox', { name: '启用规划阶段' }))
  fireEvent.click(screen.getByRole('button', { name: '校验协议' }))
  await waitFor(() => expect(protocolRequest).toHaveBeenCalledWith('/validate', 'POST', expect.objectContaining({ entry_stage: 'opinions' })))
  const raw = vi.mocked(protocolRequest).mock.calls.find(c => c[0] === '/validate')![2] as ProtocolSpec
  expect(raw.stages.map(s => s.kind)).toEqual(['propose', 'review'])
  expect(Object.keys(raw.slots)).not.toContain('planner')
  expect(raw.stages.every(s => !s.reads.includes('plan'))).toBe(true)
  const restored = togglePlanner(raw, true, catalog.items.find(t => t.template_id === 'multi_expert')!.spec as unknown as ProtocolSpec)
  expect(restored.stages.map(s => s.kind)).toEqual(['plan', 'propose', 'review'])
  expect(restored.stages[0].routes[0].target).toBe('opinions')
})

test('loads and saves V1 without upgrading its language or fields', async () => {
  const item = { protocol_id: 'old', draft_revision: 1, archived: false, versions: [], draft: { name: 'Old V1', description: '', spec, revision: 1 } }
  vi.mocked(protocolRequest).mockImplementation(async (path, method) => {
    if (path === '/templates') return catalog as never
    if (path === '') return { items: [item] } as never
    if (method === 'PUT') return item as never
    return item as never
  })
  render(<ProtocolsView disabled={false} />)
  fireEvent.click(await screen.findByText('Old V1'))
  fireEvent.change(screen.getByLabelText('协议名称'), { target: { value: 'Updated V1' } })
  fireEvent.click(screen.getByRole('button', { name: '保存协议草稿' }))
  await waitFor(() => expect(protocolRequest).toHaveBeenCalledWith('/old/draft', 'PUT', expect.objectContaining({ spec })))
})

test('keeps a system bound to an older saved protocol revision when a newer draft exists', async () => {
  const old = { protocol_id: 'p1', revision: 1, name: 'Frozen protocol', description: '', spec, spec_sha256: 'old' }
  const current = { ...old, revision: 2, name: 'New protocol' }
  vi.mocked(protocolRequest).mockResolvedValue({ items: [{ protocol_id: 'p1', draft_revision: 2, archived: false, draft: current, versions: [] }] })
  const onChange = vi.fn()
  render(<ProtocolBinding fields={{ name: 'System', description: '', workflow_id: 'p1', source_id: null, source_version: null,
    protocol_ref: { protocol_id: 'p1', revision: 1 }, protocol_bindings: { planner: [] }, agents: [] }} frozen={old} disabled={false} onChange={vi.fn()} onSelect={onChange} />)
  expect(screen.getByRole('combobox', { name: '执行协议' }).closest('.ant-select')).toHaveTextContent('Frozen protocol · 草稿 r1')
  expect(onChange).not.toHaveBeenCalled()
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '执行协议' }))
  fireEvent.click(await screen.findByText('New protocol · 草稿 r2'))
  expect(onChange).toHaveBeenCalledWith(current)
})

test('displays original and revised outputs of the same expert as separate calls', async () => {
  function call(id: string, index: number, answer: string, kind: string): ProtocolCall {
    return { call_id: id, agent_id: 'expert_a', agent_name: 'Expert A', stage_id: kind, round: 1, task_id: id,
      invocation_index: index, kind, status: 'completed', input: { question: 'Q', task: { instruction: 'Check evidence' }, reads: [] },
      usage: { call_count: 1, total_tokens: 10 }, output: { answer, ...(kind === 'revise' ? { change_note: 'Changed after evidence review',
        responses: [{ critique_call_id: 'critique_call', accepted: true, response: 'Confirmed evidence' }] } : {}) } }
  }
  render(<ProtocolCalls calls={{ revised_call: call('revised_call', 2, 'Revised answer', 'revise'), initial_call: call('initial_call', 1, 'Original answer', 'propose') }} />)
  fireEvent.click(screen.getByText('propose · 第 1 轮 · Expert A · propose'))
  fireEvent.click(screen.getByText('revise · 第 1 轮 · Expert A · revise'))
  expect(await screen.findByText('观点：Original answer')).toBeInTheDocument()
  expect(screen.getByText('观点：Revised answer')).toBeInTheDocument()
  expect(screen.getByText('修订说明：Changed after evidence review')).toBeInTheDocument()
  expect(screen.getByText('回应质疑调用：critique_call')).toBeInTheDocument()
})
