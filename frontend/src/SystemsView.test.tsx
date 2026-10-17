import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SystemsView from './SystemsView'
import { getManagedSystem, getManagedSystems, publishManagedSystem, saveManagedSystem } from './api'
import type { ManagedSystem, SystemOptions } from './types'

vi.mock('./api', () => ({ getManagedSystems: vi.fn(), getManagedSystem: vi.fn(), saveManagedSystem: vi.fn(), publishManagedSystem: vi.fn() }))

const item: ManagedSystem = {
  system_id: 'sys_a', draft_revision: 1, created_at: 'now', updated_at: 'now', versions: [],
  draft: { name: 'Research system', description: 'Notes', workflow_id: 'workflow', source_id: 'source', source_version: 1,
    snapshot: { config_sha256: 'a'.repeat(64), agents: { planner_v0: { agent_name: '规划智能体', role: 'planner',
      model_config: { model_name: 'test-model', system_prompt: 'Frozen prompt', temperature: 0.3, max_tokens: 2048 } } },
      workflow_spec: { protocol_id: 'workflow', method_id: 'method', entry_node: 'planner_v0', agents: [], edges: [], max_cycles: {} },
      protocol: { protocol_name: '现有流程' }, source: { source_id: 'source', source_type: 'document_corpus', name: 'Shared notes', version: 1 } } },
}
const options: SystemOptions = { system_id: 'legacy', system_version: '0.3', default_workflow_id: 'workflow', default_source_id: 'source',
  workflows: [{ workflow_id: 'workflow', name: '现有流程', description: '', method_id: 'method', entry_node: '', agents: [], edges: [], retry_limits: {} }],
  sources: [{ source_id: 'source', name: 'Shared notes', source_type: 'document_corpus', is_demo: false, record_id: null, question_type: null, level: null, context_count: null, supporting_fact_count: null, example_question: null, versions: [1, 2] }] }

beforeEach(() => { vi.mocked(getManagedSystems).mockResolvedValue({ items: [structuredClone(item)] }) })
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('creates a persisted draft and requires saving changes before a test run', async () => {
  const onUse = vi.fn()
  vi.mocked(saveManagedSystem).mockResolvedValue(item)
  render(<SystemsView options={options} disabled={false} onUse={onUse} onHistory={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '新建系统' }))
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: 'Research system' } })
  fireEvent.click(screen.getByRole('button', { name: '创建草稿' }))
  await waitFor(() => expect(screen.getByText('已保存草稿 r1')).toBeInTheDocument())
  expect(saveManagedSystem).toHaveBeenCalledWith(expect.objectContaining({ name: 'Research system' }), undefined, undefined)
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: 'Edited' } })
  expect(screen.getByRole('button', { name: '试运行已保存草稿' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '发布版本' })).toBeDisabled()
  expect(screen.getByText('有未保存修改')).toBeInTheDocument()
  expect(onUse).not.toHaveBeenCalled()
})

it('distinguishes draft runs, published runs and system history', async () => {
  const onUse = vi.fn(), onHistory = vi.fn()
  const published = { ...item.draft, version: 'v1', draft_revision: 1 }
  vi.mocked(publishManagedSystem).mockResolvedValue(published)
  vi.mocked(getManagedSystem).mockResolvedValue({ ...item, versions: [published] })
  render(<SystemsView options={options} disabled={false} onUse={onUse} onHistory={onHistory} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  fireEvent.click(screen.getByRole('button', { name: '试运行已保存草稿' }))
  expect(onUse).toHaveBeenLastCalledWith(expect.objectContaining({ systemId: 'sys_a', draftRevision: 1 }))
  fireEvent.click(screen.getByRole('button', { name: '发布版本' }))
  await screen.findByText('已发布 v1，此版本配置不可覆盖。')
  fireEvent.click(screen.getByRole('button', { name: '使用此版本问答' }))
  expect(onUse).toHaveBeenLastCalledWith(expect.objectContaining({ systemId: 'sys_a', systemVersion: 'v1' }))
  fireEvent.click(screen.getByRole('button', { name: '查看此系统历史' }))
  expect(onHistory).toHaveBeenCalledWith('sys_a')
})

it('shows revision conflicts and offers an explicit reload', async () => {
  vi.mocked(saveManagedSystem).mockRejectedValue(new Error('草稿已发生修改，请重新读取后再保存。'))
  vi.mocked(getManagedSystem).mockResolvedValue({ ...item, draft_revision: 2 })
  render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: 'Edited' } })
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await screen.findByText('草稿已发生修改，请重新读取后再保存。')
  fireEvent.click(screen.getByRole('button', { name: '重新读取草稿' }))
  await screen.findByText('已保存草稿 r2')
})
