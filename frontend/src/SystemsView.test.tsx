import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SystemsView from './SystemsView'
import { deleteManagedSystem, getAgentTemplates, getManagedSystem, getManagedSystems, publishManagedSystem, saveManagedSystem, protocolRequest } from './api'
import type { AgentTemplate, ManagedSystem, SystemOptions } from './types'
import type { ManagedProtocol, ProtocolCatalog } from './protocolTypes'
import catalogFixture from './protocolCatalog.fixture.json'
import { protocolDefaults, retainedBindings } from './systemProtocol'
const catalog = catalogFixture as unknown as ProtocolCatalog
const protocols: ManagedProtocol[] = catalog.items.map(t => ({ protocol_id: t.template_id, draft_revision: 1, archived: false, versions: [],
  draft: { protocol_id: t.template_id, revision: 1, name: t.name, description: '', spec_sha256: 'hash', spec: t.spec } }))

vi.mock('./api', () => ({ deleteManagedSystem: vi.fn(), protocolRequest: vi.fn().mockResolvedValue({ items: [] }), getAgentTemplates: vi.fn(), getManagedSystems: vi.fn(), getManagedSystem: vi.fn(), saveManagedSystem: vi.fn(), publishManagedSystem: vi.fn() }))

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

const templates: AgentTemplate[] = (['planner', 'retriever', 'answer_validator', 'expert', 'lead'] as const).map(role => ({
  agent_type: role, name: role, uses_llm: role !== 'retriever', uses_knowledge: role === 'retriever' || role === 'expert', editable_fields: [],
  defaults: { agent_id: `${role}_1`, agent_type: role, name: role, description: '', top_k: 3, source_id: null, source_version: null,
    model_config: role === 'retriever' ? null : { model_name: 'test-model', system_prompt: 'Prompt', temperature: 0, max_tokens: 1024 } },
}))
beforeEach(() => {
  vi.mocked(protocolRequest).mockResolvedValue({ items: structuredClone(protocols) })
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [structuredClone(item)] })
  vi.mocked(getAgentTemplates).mockResolvedValue({ items: templates })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('deletes only after confirmation and clears the selected system', async () => {
  const onDeleted = vi.fn()
  vi.mocked(deleteManagedSystem).mockResolvedValue({ deleted: true, system_id: item.system_id })
  render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} onDeleted={onDeleted} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  fireEvent.click(screen.getByRole('button', { name: '删除系统' }))
  expect(deleteManagedSystem).not.toHaveBeenCalled()
  expect(screen.getByRole('dialog')).toHaveTextContent('已有运行历史仍会保留')
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^取\s*消$/ }))
  expect(deleteManagedSystem).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '删除系统' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认删除' }))
  await screen.findByText('已删除「Research system」。已有运行历史仍保留。')
  expect(deleteManagedSystem).toHaveBeenCalledWith('sys_a', 1)
  expect(onDeleted).toHaveBeenCalledWith('sys_a')
  expect(screen.queryByRole('button', { name: /Research system/ })).not.toBeInTheDocument()
  expect(screen.queryByLabelText('系统名称')).not.toBeInTheDocument()
})

it('preserves the system and edits when deletion fails', async () => {
  vi.mocked(deleteManagedSystem).mockRejectedValue(new Error('草稿已发生修改，请重新读取后再删除。'))
  render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: 'Unsaved name' } })
  fireEvent.click(screen.getByRole('button', { name: '删除系统' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认删除' }))
  await waitFor(() => expect(screen.getByRole('dialog')).toHaveTextContent('草稿已发生修改，请重新读取后再删除。'))
  expect(screen.getByDisplayValue('Unsaved name')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /Research system/ })).toBeInTheDocument()
})

it('disables deletion while a run is active', async () => {
  const { rerender } = render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  rerender(<SystemsView options={options} disabled onUse={vi.fn()} onHistory={vi.fn()} />)
  expect(screen.getByRole('button', { name: '删除系统' })).toBeDisabled()
})

it('keeps protocol icon actions beside the picker and exposes hover labels', async () => {
  const onManageProtocols = vi.fn()
  render(<SystemsView options={options} disabled={false} onManageProtocols={onManageProtocols} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  const row = screen.getByRole('combobox', { name: '执行协议' }).closest('.protocol-picker-row')!
  const refresh = within(row as HTMLElement).getByRole('button', { name: '刷新协议列表' })
  const manage = within(row as HTMLElement).getByRole('button', { name: '创建或管理协作协议' })
  expect(refresh).toHaveTextContent('')
  expect(manage).toHaveTextContent('')
  await waitFor(() => expect(refresh).toBeEnabled())
  fireEvent.mouseEnter(refresh)
  expect(await screen.findByRole('tooltip')).toHaveTextContent('刷新协议列表')
  fireEvent.click(manage)
  expect(onManageProtocols).toHaveBeenCalledOnce()
})

it('creates a persisted draft and requires saving changes before a test run', async () => {
  const onUse = vi.fn()
  vi.mocked(saveManagedSystem).mockResolvedValue(item)
  render(<SystemsView options={options} disabled={false} onUse={onUse} onHistory={vi.fn()} />)
  await waitFor(() => expect(screen.getByRole('button', { name: '新建系统' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '新建系统' }))
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: 'Research system' } })
  await chooseProtocol('固定流水线')
  fireEvent.click(screen.getByRole('button', { name: '创建草稿' }))
  await waitFor(() => expect(screen.getByText('已保存草稿 r1')).toBeInTheDocument())
  expect(saveManagedSystem).toHaveBeenCalledWith(expect.objectContaining({ name: 'Research system' }), undefined, undefined)
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: 'Edited' } })
  expect(screen.getByRole('button', { name: '试运行已保存草稿' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '发布版本' })).toBeDisabled()
  expect(screen.getByText('有未保存修改')).toBeInTheDocument()
  expect(onUse).not.toHaveBeenCalled()
})

it('creates, copies and removes distinct expert instances and saves their independent parameters', async () => {
  render(<SystemsView options={options} disabled={false} onManageProtocols={vi.fn()} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  await screen.findByRole('button', { name: '添加领域专家' })
  fireEvent.click(screen.getByRole('button', { name: '添加领域专家' }))
  fireEvent.click(screen.getByText('expert · 领域专家'))
  const name = screen.getByDisplayValue('expert')
  fireEvent.change(name, { target: { value: '医学专家' } })
  fireEvent.change(screen.getByDisplayValue('Prompt'), { target: { value: '独立提示词' } })
  fireEvent.click(screen.getByRole('button', { name: '复制实例' }))
  expect(screen.getByText('医学专家 副本 · 领域专家')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '删除实例' }))
  expect(screen.queryByText('医学专家 · 领域专家')).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('医学专家 副本 · 领域专家'))
  fireEvent.click(screen.getByRole('button', { name: '复制实例' }))
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: 'New system' } })
  vi.mocked(saveManagedSystem).mockResolvedValue(item)
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await waitFor(() => expect(saveManagedSystem).toHaveBeenCalled())
  const saved = vi.mocked(saveManagedSystem).mock.calls.at(-1)![0]
  const experts = saved.agents!.filter(a => a.agent_type === 'expert')
  expect(experts).toHaveLength(2)
  expect(experts[0].agent_id).not.toBe(experts[1].agent_id)
  expect(experts[1].model_config?.system_prompt).toBe('独立提示词')
})

it('copies a frozen instance from another system with a fresh id', async () => {
  const second: ManagedSystem = { ...item, system_id: 'sys_b', draft: { ...item.draft, name: 'Other system' } }
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [item, second] })
  render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '从其他系统复制实例' }))
  fireEvent.click(await screen.findByText('Other system · 规划智能体', { selector: '.ant-select-item-option-content' }))
  vi.mocked(saveManagedSystem).mockResolvedValue(item)
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await waitFor(() => expect(saveManagedSystem).toHaveBeenCalled())
  const agents = vi.mocked(saveManagedSystem).mock.calls.at(-1)![0].agents!
  expect(agents).toHaveLength(2)
  expect(agents[1].agent_id).not.toBe('planner_v0')
  expect(agents[1].model_config?.system_prompt).toBe('Frozen prompt')
})

it('keeps incomplete saved compositions out of publishing and trial runs', async () => {
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [{ ...item, draft: { ...item.draft, snapshot: {
    ...item.draft.snapshot, schema_version: '2', configuration_errors: ['缺少审核者'], workflow_spec: null,
  } } }] })
  render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  expect(screen.getByText('缺少审核者')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '发布版本' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '试运行已保存草稿' })).toBeDisabled()
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


async function chooseProtocol(name: string, revision = 1) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '执行协议' }))
  fireEvent.click(await screen.findByText(`${name} · 草稿 r${revision}`, { selector: '.ant-select-item-option-content' }))
}
async function newSystem() {
  render(<SystemsView options={options} onManageProtocols={vi.fn()} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  await waitFor(() => expect(screen.getByRole('button', { name: '新建系统' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '新建系统' }))
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: '自动配置系统' } })
}

it('requires a saved personal protocol and shows a creation entry when none exist', async () => {
  vi.mocked(protocolRequest).mockResolvedValue({ items: [] })
  await newSystem()
  expect(screen.queryByRole('combobox', { name: '协作流程' })).not.toBeInTheDocument()
  expect(screen.queryByRole('combobox', { name: '自定义协议版本' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '创建草稿' })).toBeDisabled()
  expect(await screen.findByText(/暂无个人协议/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '创建或管理协作协议' })).toBeEnabled()
  expect(screen.getByText('0 / 5')).toBeInTheDocument()
})

it('generates protocol defaults for a pristine composition and saves explicit revision and bindings', async () => {
  await newSystem()
  await chooseProtocol('固定流水线')
  expect(screen.getByText('3 / 5')).toBeInTheDocument()
  await chooseProtocol('单专家问答')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByText('1 / 5')).toBeInTheDocument()
  await chooseProtocol('多专家汇总')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByText('4 / 5')).toBeInTheDocument()
  fireEvent.click(screen.getByText('专家 A · 领域专家'))
  fireEvent.change(screen.getByDisplayValue('Prompt'), { target: { value: '自定义专家提示词' } })
  vi.mocked(saveManagedSystem).mockResolvedValue(item)
  fireEvent.click(screen.getByRole('button', { name: '创建草稿' }))
  await waitFor(() => expect(saveManagedSystem).toHaveBeenCalled())
  const saved = vi.mocked(saveManagedSystem).mock.calls.at(-1)![0]
  expect(saved.protocol_ref).toEqual({ protocol_id: 'multi_expert', revision: 1 })
  expect(saved.agents!.map(a => a.agent_type)).toEqual(['planner', 'expert', 'expert', 'answer_validator'])
  expect(new Set(saved.agents!.map(a => a.agent_id)).size).toBe(4)
  expect(saved.agents!.every(a => !a.source_id && !a.source_version)).toBe(true)
  expect(saved.protocol_bindings!.experts).toEqual(saved.agents!.filter(a => a.agent_type === 'expert').map(a => a.agent_id))
  expect(saved.agents![1].model_config?.system_prompt).toBe('自定义专家提示词')
  expect(saved.agents![2].model_config?.system_prompt).toBe('Prompt')
})

it('protects modified instances on cancel and preserve, then requires missing bindings', async () => {
  await newSystem()
  await chooseProtocol('固定流水线')
  fireEvent.click(screen.getByText('planner · 规划'))
  fireEvent.change(screen.getByDisplayValue('planner'), { target: { value: '我的规划者' } })
  await chooseProtocol('单专家问答')
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^取\s*消$/ }))
  expect(screen.getByDisplayValue('我的规划者')).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: '执行协议' }).closest('.ant-select')).toHaveTextContent('固定流水线')
  await chooseProtocol('单专家问答')
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '保留现有实例' }))
  expect(screen.getByDisplayValue('我的规划者')).toBeInTheDocument()
  expect(screen.getByText('expert：需要绑定 1～1 个 expert，当前 0 个。')).toBeInTheDocument()
  expect(saveManagedSystem).not.toHaveBeenCalled()
})

it('requires an explicit choice before replacing saved instances and keeps releases frozen', async () => {
  const system = { ...structuredClone(item), versions: [{ ...structuredClone(item.draft), version: 'v1' }] }
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [system] })
  render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  await chooseProtocol('单专家问答')
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '生成新协议默认实例' }))
  expect(screen.getByText('1 / 5')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '试运行已保存草稿' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '发布版本' })).toBeDisabled()
  expect(system.versions[0].snapshot.agents.planner_v0.model_config.system_prompt).toBe('Frozen prompt')
})

it('retains an archived draft reference and editor state when refreshing after protocol changes or failures', async () => {
  const doc = protocols[2].draft
  const defaults = protocolDefaults(doc.spec, templates)
  const system = { ...structuredClone(item), draft: { ...structuredClone(item.draft), workflow_id: doc.protocol_id,
    protocol_ref: { protocol_id: doc.protocol_id, revision: 1 }, protocol_bindings: defaults.bindings, agents: defaults.agents,
    snapshot: { ...item.draft.snapshot, schema_version: '3', protocol_document: doc } } }
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [system] })
  const { rerender } = render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: 'Unsaved name' } })
  vi.mocked(protocolRequest).mockResolvedValue({ items: [{ ...protocols[2], archived: true, draft_revision: 2, draft: { ...doc, revision: 2 } }] })
  rerender(<SystemsView options={options} disabled={false} active={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  rerender(<SystemsView options={options} disabled={false} active onUse={vi.fn()} onHistory={vi.fn()} />)
  expect(screen.getByDisplayValue('Unsaved name')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('combobox', { name: '执行协议' }).closest('.ant-select')).toHaveTextContent('草稿 r1'))
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新协议列表' })).toBeEnabled())
  vi.mocked(protocolRequest).mockRejectedValue(new Error('offline'))
  fireEvent.click(screen.getByRole('button', { name: '刷新协议列表' }))
  await screen.findByText('协议列表加载失败；保留当前明确引用')
  expect(screen.getByRole('combobox', { name: '执行协议' }).closest('.ant-select')).toHaveTextContent('草稿 r1')
  vi.mocked(saveManagedSystem).mockResolvedValue(system)
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await waitFor(() => expect(saveManagedSystem).toHaveBeenCalledWith(expect.objectContaining({ protocol_ref: { protocol_id: doc.protocol_id, revision: 1 }, agents: defaults.agents, protocol_bindings: defaults.bindings }), system.system_id, 1))
})

it('preserves instance ids, prompt, model and source, leaving ambiguous expert choices unbound', () => {
  const defaults = protocolDefaults(protocols[3].draft.spec, templates)
  defaults.agents[1].source_id = 'source'; defaults.agents[1].source_version = 1
  defaults.agents[1].model_config!.system_prompt = 'My prompt'
  const before = structuredClone(defaults.agents)
  const bindings = retainedBindings(protocols[2].draft.spec, { ...item.draft, agents: defaults.agents, protocol_bindings: defaults.bindings })
  expect(bindings.expert).toEqual([])
  const narrower = { ...protocols[3].draft.spec, slots: { ...protocols[3].draft.spec.slots, experts: { role: 'expert', minimum: 1, maximum: 1 } } }
  expect(retainedBindings(narrower, { ...item.draft, agents: defaults.agents, protocol_bindings: defaults.bindings }).experts).toEqual([])
  expect(defaults.agents).toEqual(before)
  const again = protocolDefaults(protocols[3].draft.spec, templates)
  expect(again.agents.every(a => !defaults.agents.some(b => a.agent_id === b.agent_id))).toBe(true)
})

it('runs a pinned saved protocol draft but requires a published protocol before system publishing', async () => {
  const doc = protocols[2].draft, defaults = protocolDefaults(doc.spec, templates)
  defaults.agents[0].source_id = 'source'; defaults.agents[0].source_version = 1
  const system = { ...structuredClone(item), draft: { ...structuredClone(item.draft), workflow_id: doc.protocol_id,
    protocol_ref: { protocol_id: doc.protocol_id, revision: 1 }, protocol_bindings: defaults.bindings, agents: defaults.agents,
    snapshot: { ...item.draft.snapshot, schema_version: '3', protocol_document: doc } } }
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [system] })
  vi.mocked(protocolRequest).mockResolvedValue({ items: [{ ...protocols[2], versions: [{ ...doc, version: 'v1' }] }] })
  const onUse = vi.fn()
  render(<SystemsView options={options} disabled={false} onUse={onUse} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  expect(screen.getByRole('button', { name: '试运行已保存草稿' })).toBeEnabled()
  expect(screen.getByRole('button', { name: '发布版本' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '试运行已保存草稿' }))
  expect(onUse).toHaveBeenCalledWith(expect.objectContaining({ document: expect.objectContaining({ protocol_ref: { protocol_id: doc.protocol_id, revision: 1 } }) }))
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '执行协议' }))
  expect(await screen.findByText('我的已发布协议')).toBeInTheDocument()
  expect(screen.getByText('我的协议草稿')).toBeInTheDocument()
  fireEvent.click(await screen.findByText(`${doc.name} · v1`, { selector: '.ant-select-item-option-content' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '保留现有实例' }))
  const publishedDoc = { ...doc, version: 'v1' }
  vi.mocked(saveManagedSystem).mockResolvedValue({ ...system, draft: { ...system.draft, protocol_ref: { protocol_id: doc.protocol_id, version: 'v1' }, snapshot: { ...system.draft.snapshot, protocol_document: publishedDoc } } })
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '发布版本' })).toBeEnabled())
})

it('shows a frozen prompt as a read-only code block with a JSON view and copy control', async () => {
  render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  fireEvent.click(screen.getByText('规划智能体 · test-model（只读）'))
  const promptView = await screen.findByRole('group', { name: '规划智能体 · 系统提示词' })
  expect(within(promptView).getByText('Frozen prompt')).toBeInTheDocument()
  fireEvent.click(screen.getByText('JSON'))
  expect(screen.getByRole('group', { name: '规划智能体 · JSON 配置' })).toHaveTextContent('"system_prompt": "Frozen prompt"')
})
