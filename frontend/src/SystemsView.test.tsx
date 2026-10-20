import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SystemsView from './SystemsView'
import { getAgentTemplates, getManagedSystem, getManagedSystems, publishManagedSystem, saveManagedSystem } from './api'
import type { AgentTemplate, ManagedSystem, SystemOptions } from './types'

vi.mock('./api', () => ({ getAgentTemplates: vi.fn(), getManagedSystems: vi.fn(), getManagedSystem: vi.fn(), saveManagedSystem: vi.fn(), publishManagedSystem: vi.fn() }))

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

const templates: AgentTemplate[] = (['planner', 'retriever', 'answer_validator', 'expert'] as const).map(role => ({
  agent_type: role, name: role, uses_llm: role !== 'retriever', uses_knowledge: role === 'retriever' || role === 'expert', editable_fields: [],
  defaults: { agent_id: `${role}_1`, agent_type: role, name: role, description: '', top_k: 3, source_id: null, source_version: null,
    model_config: role === 'retriever' ? null : { model_name: 'test-model', system_prompt: 'Prompt', temperature: 0, max_tokens: 1024 } },
}))
beforeEach(() => {
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [structuredClone(item)] })
  vi.mocked(getAgentTemplates).mockResolvedValue({ items: templates })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('creates a persisted draft and requires saving changes before a test run', async () => {
  const onUse = vi.fn()
  vi.mocked(saveManagedSystem).mockResolvedValue(item)
  render(<SystemsView options={options} disabled={false} onUse={onUse} onHistory={vi.fn()} />)
  await waitFor(() => expect(screen.getByRole('button', { name: '新建系统' })).toBeEnabled())
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

it('creates, copies and removes distinct expert instances and saves their independent parameters', async () => {
  render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
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

const workflowOptions: SystemOptions = { ...options, default_workflow_id: 'multi_hop_qa_fixed',
  workflows: [
    { ...options.workflows[0], workflow_id: 'multi_hop_qa_fixed', name: '固定流水线' },
    { ...options.workflows[0], workflow_id: 'multi_hop_qa_v0', name: '反馈重试流水线' },
  ],
}

async function chooseWorkflow(name: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '协作流程' }))
  fireEvent.click(await screen.findByText(name, { selector: '.ant-select-item-option-content' }))
}

async function newSystem(defaultWorkflow = 'multi_hop_qa_fixed') {
  render(<SystemsView options={{ ...workflowOptions, default_workflow_id: defaultWorkflow }}
    disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  await waitFor(() => expect(screen.getByRole('button', { name: '新建系统' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '新建系统' }))
  fireEvent.change(screen.getByLabelText('系统名称'), { target: { value: '自动配置系统' } })
}

it('automatically replaces untouched defaults across workflows and saves independent editable instances', async () => {
  await newSystem()
  expect(screen.getByText('3 / 5')).toBeInTheDocument()
  await chooseWorkflow('单专家问答')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByText('1 / 5')).toBeInTheDocument()
  expect(screen.getByText('expert：知识源待配置。')).toBeInTheDocument()
  await chooseWorkflow('多专家汇总')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByText('4 / 5')).toBeInTheDocument()
  expect(screen.getByText('专家 A · 领域专家')).toBeInTheDocument()
  expect(screen.getByText('专家 B · 领域专家')).toBeInTheDocument()
  expect(screen.getByText('串行 · 独立作答')).toBeInTheDocument()
  fireEvent.click(screen.getByText('专家 A · 领域专家'))
  fireEvent.change(screen.getByDisplayValue('Prompt'), { target: { value: '自定义专家提示词' } })
  vi.mocked(saveManagedSystem).mockResolvedValue(item)
  fireEvent.click(screen.getByRole('button', { name: '创建草稿' }))
  await waitFor(() => expect(saveManagedSystem).toHaveBeenCalled())
  const saved = vi.mocked(saveManagedSystem).mock.calls.at(-1)![0]
  expect(saved.workflow_id).toBe('multi_expert')
  expect(saved.execution_mode).toBe('sequential')
  expect(saved.agents!.map(a => a.agent_type)).toEqual(['planner', 'expert', 'expert', 'answer_validator'])
  expect(new Set(saved.agents!.map(a => a.agent_id)).size).toBe(4)
  expect(saved.agents!.every(a => !a.source_id && !a.source_version)).toBe(true)
  expect(saved.agents![1].model_config?.system_prompt).toBe('自定义专家提示词')
  expect(saved.agents![2].model_config?.system_prompt).toBe('Prompt')
})

it('initializes a new system using its selected default workflow', async () => {
  await newSystem('single_expert')
  expect(screen.getByText('1 / 5')).toBeInTheDocument()
  expect(screen.getByText('expert · 领域专家')).toBeInTheDocument()
  expect(screen.queryByText('planner · 规划')).not.toBeInTheDocument()
})

it('protects custom edits with cancel and retain choices and immediately explains incompatible roles', async () => {
  await newSystem()
  fireEvent.click(screen.getByText('planner · 规划'))
  fireEvent.change(screen.getByDisplayValue('planner'), { target: { value: '我的规划者' } })
  await chooseWorkflow('单专家问答')
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^取\s*消$/ }))
  expect(screen.getByDisplayValue('我的规划者')).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: '协作流程' }).closest('.ant-select')).toHaveTextContent('固定流水线')
  await chooseWorkflow('单专家问答')
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '保留现有实例' }))
  expect(screen.getByDisplayValue('我的规划者')).toBeInTheDocument()
  expect(screen.getByText('缺少领域专家：需要至少 1 个，当前 0 个。')).toBeInTheDocument()
  expect(screen.getByText(/规划者超出要求.*我的规划者/)).toBeInTheDocument()
  expect(saveManagedSystem).not.toHaveBeenCalled()
  expect(publishManagedSystem).not.toHaveBeenCalled()
})

it('requires an explicit choice before replacing a saved composition and leaves its release untouched', async () => {
  const system = { ...structuredClone(item), versions: [{ ...structuredClone(item.draft), version: 'v1' }] }
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [system] })
  render(<SystemsView options={workflowOptions} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  await waitFor(() => expect(screen.getByRole('combobox', { name: '协作流程' })).not.toBeDisabled())
  await chooseWorkflow('单专家问答')
  expect(screen.getByRole('dialog')).toBeInTheDocument()
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '使用新流程默认配置' }))
  expect(screen.getByText('1 / 5')).toBeInTheDocument()
  expect(screen.getByText('expert · 领域专家')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '试运行已保存草稿' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '发布版本' })).toBeDisabled()
  expect(system.versions[0].snapshot.agents.planner_v0.model_config.system_prompt).toBe('Frozen prompt')
  expect(system.draft.workflow_id).toBe('workflow')
  expect(saveManagedSystem).not.toHaveBeenCalled()
})

it('shows a frozen prompt as a read-only code block with a JSON view and copy control', async () => {
  render(<SystemsView options={options} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  fireEvent.click(screen.getByText('规划智能体 · test-model（只读）'))
  const promptView = await screen.findByRole('group', { name: '规划智能体 · 系统提示词' })
  expect(within(promptView).getByText('Frozen prompt')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '复制提示词' })).toBeInTheDocument()
  expect(screen.getByText('温度 0.3')).toBeInTheDocument()
  fireEvent.click(screen.getByText('JSON'))
  expect(screen.getByRole('group', { name: '规划智能体 · JSON 配置' })).toHaveTextContent('"system_prompt": "Frozen prompt"')
})

it('preserves ids, prompts and source bindings when switching between the two pipelines', async () => {
  const agents = templates.filter(t => t.agent_type !== 'expert').map(t => structuredClone(t.defaults))
  agents[0].model_config!.system_prompt = '已有规划提示词'
  agents[1].source_id = 'source'; agents[1].source_version = 1; agents[1].top_k = 7
  const system = { ...structuredClone(item), draft: { ...structuredClone(item.draft),
    workflow_id: 'multi_hop_qa_fixed', agents, execution_mode: 'sequential' as const } }
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [system] })
  render(<SystemsView options={workflowOptions} disabled={false} onUse={vi.fn()} onHistory={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Research system/ }))
  await chooseWorkflow('反馈重试流水线')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  vi.mocked(saveManagedSystem).mockResolvedValue(system)
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await waitFor(() => expect(saveManagedSystem).toHaveBeenCalled())
  expect(vi.mocked(saveManagedSystem).mock.calls.at(-1)![0]).toEqual(expect.objectContaining({
    workflow_id: 'multi_hop_qa_v0', agents, execution_mode: 'sequential',
  }))
})
