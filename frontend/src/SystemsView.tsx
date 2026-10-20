import { useEffect, useState } from 'react'
import { Alert, Button, Card, Collapse, Empty, Input, Modal, Select, Tag } from 'antd'
import { getAgentTemplates, getManagedSystem, getManagedSystems, publishManagedSystem, saveManagedSystem } from './api'
import { AgentEditor, newInstance } from './AgentEditor'
import { PromptBlock } from './PromptBlock'
import type { AgentInstance, AgentTemplate, ManagedSelection, ManagedSystem, SystemDocument, SystemDraftFields, SystemOptions } from './types'

const PIPELINES = ['multi_hop_qa_fixed', 'multi_hop_qa_v0']
const DEFAULT_ROLES: Record<string, AgentInstance['agent_type'][]> = {
  multi_hop_qa_fixed: ['planner', 'retriever', 'answer_validator'],
  multi_hop_qa_v0: ['planner', 'retriever', 'answer_validator'],
  single_expert: ['expert'],
  multi_expert: ['planner', 'expert', 'expert', 'answer_validator'],
}

function defaultAgents(workflow: string | null, templates: AgentTemplate[]): AgentInstance[] {
  return (DEFAULT_ROLES[workflow ?? ''] ?? []).map((role, index) => {
    const template = templates.find(t => t.agent_type === role)
    if (!template) throw new Error('智能体模板尚未加载完整，请刷新后重试。')
    const agent = newInstance(template)
    if (workflow === 'multi_expert' && role === 'expert') agent.name = `专家 ${index === 1 ? 'A' : 'B'}`
    return { ...agent, source_id: null, source_version: null }
  })
}

function configurationGaps(fields: SystemDraftFields, options?: SystemOptions): string[] {
  if (!fields.workflow_id) return ['请选择协作流程。']
  if (!DEFAULT_ROLES[fields.workflow_id]) return []
  const agents = fields.agents ?? []
  const gaps: string[] = []
  const counts = (role: AgentInstance['agent_type']) => agents.filter(a => a.agent_type === role).length
  const labels = { planner: '规划者', retriever: '检索者', answer_validator: '审核者', expert: '领域专家' }
  const limits: Record<AgentInstance['agent_type'], [number, number]> = fields.workflow_id === 'multi_expert'
    ? { planner: [0, 1], retriever: [0, 0], answer_validator: [1, 1], expert: [1, 3] }
    : fields.workflow_id === 'single_expert'
      ? { planner: [0, 0], retriever: [0, 0], answer_validator: [0, 0], expert: [1, 1] }
      : { planner: [1, 1], retriever: [1, 1], answer_validator: [1, 1], expert: [0, 0] }
  for (const role of Object.keys(limits) as AgentInstance['agent_type'][]) {
    const [min, max] = limits[role], count = counts(role)
    if (count < min) gaps.push(`缺少${labels[role]}：需要至少 ${min} 个，当前 ${count} 个。`)
    if (count > max) gaps.push(`${labels[role]}超出要求：最多 ${max} 个，当前 ${count} 个（${agents.filter(a => a.agent_type === role).map(a => a.name).join('、')}）。`)
  }
  for (const agent of agents.filter(a => a.agent_type === 'expert' || a.agent_type === 'retriever')) {
    if (!agent.source_id) gaps.push(`${agent.name}：知识源待配置。`)
    else if (options?.sources.find(s => s.source_id === agent.source_id)?.source_type === 'document_corpus' && !agent.source_version) {
      gaps.push(`${agent.name}：请选择固定知识源版本。`)
    }
  }
  return gaps
}

function fieldsOf(document: SystemDocument): SystemDraftFields {
  const { name, description, workflow_id, source_id, source_version } = document
  const agents = document.agents ?? document.snapshot.instances ?? Object.entries(document.snapshot.agents).map(([id, a]) => ({
    agent_id: id, agent_type: a.role as AgentInstance['agent_type'], name: a.agent_name, description: a.description ?? '',
    model_config: a.role === 'retriever' ? null : a.model_config,
    source_id: a.role === 'retriever' ? document.source_id : null,
    source_version: a.role === 'retriever' ? document.source_version : null, top_k: a.retrieval_config?.top_k ?? 3,
  }))
  return { name, description, workflow_id, source_id, source_version, agents, execution_mode: document.execution_mode ?? 'sequential' }
}

export function SystemConfigPreview({ document }: { document: SystemDocument }) {
  return <div className="system-config-preview">
    <p>流程：{document.workflow_id ? document.snapshot.protocol.protocol_name ?? document.workflow_id : '尚未选择'} · 知识源：{document.snapshot.source?.name ?? (Object.keys(document.snapshot.sources ?? {}).length ? '按实例绑定固定版本' : '尚未选择')}{document.source_version ? ` · 版本 ${document.source_version}` : ''}</p>
    <Collapse items={Object.entries(document.snapshot.agents).map(([id, agent]) => ({ key: id,
      label: `${agent.agent_name} · ${agent.role === 'retriever' ? '本地 BM25' : agent.model_config.model_name}（只读）`,
      children: <>{agent.role !== 'retriever' ? <>
        <div className="prompt-facts">
          <span>温度 {agent.model_config.temperature}</span>
          <span>Token 上限 {agent.model_config.max_tokens}</span>
          <span>{agent.model_config.model_name}</span>
        </div>
        <PromptBlock agentId={id} agentName={agent.agent_name} role={agent.role} model={agent.model_config}
          source={document.snapshot.sources?.[id]} />
      </> : <p>确定性检索节点</p>}
        {document.snapshot.sources?.[id] ? <p>知识源：{document.snapshot.sources[id].name} · {document.snapshot.sources[id].version ? `版本 ${document.snapshot.sources[id].version}` : '固定内容哈希'}</p> : null}</>,
    }))} />
    <small>配置指纹：{document.snapshot.config_sha256}</small>
  </div>
}

export default function SystemsView({ options, disabled, onUse, onHistory }: {
  options?: SystemOptions; disabled: boolean; onUse: (selection: ManagedSelection) => void; onHistory: (id: string) => void
}) {
  const empty: SystemDraftFields = { name: '', description: '', workflow_id: options?.default_workflow_id ?? null, source_id: null, source_version: null }
  const [systems, setSystems] = useState<ManagedSystem[]>([])
  const [selected, setSelected] = useState<ManagedSystem>()
  const [fields, setFields] = useState<SystemDraftFields>(empty)
  const [version, setVersion] = useState<string>()
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [templates, setTemplates] = useState<AgentTemplate[]>([])
  const [templateError, setTemplateError] = useState('')
  const [editing, setEditing] = useState(false)
  // Only automatically replace a composition generated in this editing session.
  // Saved drafts may contain historical defaults and must not be silently reset.
  const [generatedAgents, setGeneratedAgents] = useState<string>()
  const [pendingWorkflow, setPendingWorkflow] = useState<string>()
  const dirty = selected ? JSON.stringify(fields) !== JSON.stringify(fieldsOf(selected.draft)) : Boolean(fields.name || fields.description || fields.source_id || (editing && fields.agents?.length))
  const source = options?.sources.find(item => item.source_id === fields.source_id)
  const release = selected?.versions.find(item => item.version === version)
  const issues = selected?.draft.snapshot.configuration_errors ?? []
  const currentGaps = configurationGaps(fields, options)
  const ready = Boolean(selected && !dirty && !currentGaps.length && fields.workflow_id &&
    (selected.draft.snapshot.schema_version === '2' ? !issues.length && selected.draft.snapshot.workflow_spec : selected.draft.snapshot.source))

  useEffect(() => {
    const controller = new AbortController()
    getManagedSystems(controller.signal).then(data => setSystems(data.items)).catch((reason: unknown) => {
      if (!(reason instanceof Error && reason.name === 'AbortError')) setError(String(reason))
    })
    getAgentTemplates(controller.signal).then(data => setTemplates(data.items)).catch((reason: unknown) => {
      if (!(reason instanceof Error && reason.name === 'AbortError')) setTemplateError('智能体模板加载失败，请刷新后重试。')
    })
    return () => controller.abort()
  }, [])

  function adopt(system?: ManagedSystem) {
    if (dirty && !window.confirm('草稿有未保存的修改，确定放弃这些修改吗？')) return
    let next: SystemDraftFields
    try {
      next = system ? fieldsOf(system.draft) : { ...empty,
        agents: defaultAgents(empty.workflow_id, templates), execution_mode: 'sequential' }
    } catch (reason) { setError((reason as Error).message); return }
    setSelected(system); setFields(next)
    setGeneratedAgents(system ? undefined : JSON.stringify(next.agents)); setPendingWorkflow(undefined)
    setVersion(system?.versions[0]?.version); setEditing(true); setError(''); setNotice('')
  }

  function applyWorkflow(workflow: string | null, replace: boolean) {
    try {
      const agents = replace ? defaultAgents(workflow, templates) : fields.agents
      setFields({ ...fields, workflow_id: workflow, agents, execution_mode: 'sequential',
        ...(replace ? { source_id: null, source_version: null } : {}) })
      if (replace) setGeneratedAgents(JSON.stringify(agents))
      setPendingWorkflow(undefined); setError(''); setNotice('')
    } catch (reason) { setError((reason as Error).message) }
  }

  function changeWorkflow(workflow: string | null) {
    if (workflow === fields.workflow_id) return
    if (!workflow || (PIPELINES.includes(workflow) && PIPELINES.includes(fields.workflow_id ?? ''))) {
      applyWorkflow(workflow, false)
    } else if (!fields.agents?.length || JSON.stringify(fields.agents) === generatedAgents) {
      applyWorkflow(workflow, true)
    } else {
      setPendingWorkflow(workflow)
    }
  }

  async function action(work: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await work() } catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败，请重试。') }
    finally { setBusy(false) }
  }

  async function save() {
    await action(async () => {
      const saved = await saveManagedSystem(fields, selected?.system_id, selected?.draft_revision)
      setSelected(saved); setFields(fieldsOf(saved.draft)); setSystems((await getManagedSystems()).items)
      setNotice(`已保存草稿 r${saved.draft_revision}；尚未发布本次修改。`)
    })
  }

  function use(document: SystemDocument, published: boolean) {
    if (!selected) return
    onUse({ systemId: selected.system_id, name: document.name, document,
      ...(published ? { systemVersion: document.version } : { draftRevision: selected.draft_revision }) })
  }

  const locked = disabled || busy
  return <main className="systems-workspace">
    <header className="history-hero"><div><span className="section-kicker">MY QA SYSTEMS</span><h1>我的系统</h1><p>组合现有流程与共享知识源，保存草稿并发布可追溯的版本。</p></div>
      <Button disabled={locked || !templates.length} onClick={() => adopt()}>新建系统</Button></header>
    {templateError ? <Alert type="error" title={templateError} /> : null}
    {error ? <Alert type="error" title="系统操作未完成" description={error} action={selected ? <Button onClick={() => void action(async () => {
      const current = await getManagedSystem(selected.system_id); setSelected(current); setFields(fieldsOf(current.draft)); setNotice('已重新读取服务器草稿。')
      setGeneratedAgents(undefined); setPendingWorkflow(undefined)
    })}>重新读取草稿</Button> : undefined} /> : null}
    {notice ? <Alert type="success" title={notice} /> : null}
    <div className="systems-grid"><aside className="systems-list" aria-label="我的系统列表">
      {systems.length ? systems.map(item => <button key={item.system_id} type="button" disabled={locked} className={selected?.system_id === item.system_id ? 'selected' : ''} onClick={() => adopt(item)}>
        <strong>{item.draft.name}</strong><span>草稿 r{item.draft_revision} · {item.versions[0]?.version ?? '未发布'}</span>
      </button>) : <Empty description="尚未创建系统" />}
    </aside><div className="systems-editor">
      {!editing ? <Empty description="选择已有系统，或点击新建系统。" /> : <>
        <Card title={selected ? '编辑系统草稿' : '创建系统草稿'} extra={dirty ? <Tag color="orange">有未保存修改</Tag> : <Tag>已保存</Tag>}>
          <div className="system-fields">
            <label htmlFor="system-name">系统名称</label><Input id="system-name" value={fields.name} maxLength={100} disabled={locked} onChange={event => setFields({ ...fields, name: event.target.value })} />
            <label htmlFor="system-description">系统说明</label><Input.TextArea id="system-description" value={fields.description} maxLength={2000} disabled={locked} onChange={event => setFields({ ...fields, description: event.target.value })} />
            <label htmlFor="system-workflow">协作流程</label><Select id="system-workflow" value={fields.workflow_id} allowClear disabled={locked || !templates.length} placeholder="选择执行模板" options={[
              ...(options?.workflows.map(item => ({ value: item.workflow_id, label: item.name })) ?? []),
              { value: 'single_expert', label: '单专家问答' }, { value: 'multi_expert', label: '多专家汇总' },
            ]} onChange={value => changeWorkflow(value ?? null)} />
            {fields.workflow_id === 'multi_expert' ? <><label htmlFor="execution-mode">专家执行方式</label><Select id="execution-mode" value={fields.execution_mode ?? 'sequential'} disabled={locked}
              options={[{ value: 'sequential', label: '串行 · 独立作答' }, { value: 'parallel', label: '并行 · 全部完成后审核' }]}
              onChange={value => setFields({ ...fields, execution_mode: value })} /></> : null}
            {!fields.agents ? <>
            <label htmlFor="system-source">知识源</label><Select id="system-source" value={fields.source_id} allowClear disabled={locked} placeholder="选择工作区共享材料" options={options?.sources.map(item => ({ value: item.source_id, label: item.name }))} onChange={value => setFields({ ...fields, source_id: value ?? null, source_version: null })} />
            {source?.source_type === 'document_corpus' ? <><label htmlFor="system-source-version">固定知识源版本</label><Select id="system-source-version" value={fields.source_version} disabled={locked} placeholder="选择具体版本" options={source.versions?.map(value => ({ value, label: `版本 ${value}` }))} onChange={value => setFields({ ...fields, source_version: value })} /></> : null}
            </> : null}
          </div>
          <p className="agent-role-requirement">{!fields.workflow_id ? '选择协作流程后，可生成对应的默认实例。' : fields.workflow_id === 'single_expert' ? '需要：1 个领域专家。' : fields.workflow_id === 'multi_expert' ? '需要：1～3 个领域专家、1 个审核者；可选 1 个规划者。' : '需要：规划、BM25 检索、回答与审核各 1 个。'}</p>
          <p>默认实例可继续编辑；切换流程后需保存草稿，知识源由你指定。</p>
          <AgentEditor agents={fields.agents ?? []} templates={templates} options={options} disabled={locked}
            reusableAgents={systems.filter(s => s.system_id !== selected?.system_id).flatMap(s => (fieldsOf(s.draft).agents ?? []).map(a => ({
              key: `${s.system_id}:${a.agent_id}`, label: `${s.draft.name} · ${a.name}`, agent: a,
            })))}
            onChange={agents => setFields({ ...fields, agents, source_id: null, source_version: null })} />
          {!dirty && issues.length ? <Alert type="warning" title="草稿已保存，仍需补齐配置" description={issues.join('；')} /> : null}
          {(dirty || !issues.length) && currentGaps.length ? <Alert type="warning" title="当前配置仍需补齐"
            description={<ul>{currentGaps.map(gap => <li key={gap}>{gap}</li>)}</ul>} /> : null}
          <div className="system-actions"><Button type="primary" disabled={locked || !fields.name.trim()} onClick={() => void save()}>{selected ? '保存草稿' : '创建草稿'}</Button>
            <Button disabled={locked || !ready} onClick={() => selected && use(selected.draft, false)}>试运行已保存草稿</Button>
            <Button disabled={locked || !ready} onClick={() => void action(async () => {
              if (!selected) return
              const published = await publishManagedSystem(selected.system_id, selected.draft_revision)
              const current = await getManagedSystem(selected.system_id)
              setSelected(current); setVersion(published.version); setSystems((await getManagedSystems()).items)
              setNotice(`已发布 ${published.version}，此版本配置不可覆盖。`)
            })}>发布版本</Button>
            {selected ? <Button disabled={locked} onClick={() => onHistory(selected.system_id)}>查看此系统历史</Button> : null}
          </div>
          {dirty ? <p>先保存修改，再试运行或发布。下方预览为上次保存的配置。</p> : null}
          {selected ? <><h3>已保存草稿 r{selected.draft_revision}</h3><SystemConfigPreview document={selected.draft} /></> : null}
        </Card>
        {selected?.versions.length ? <Card title="已发布版本（只读）">
          <Select aria-label="发布版本" value={version} onChange={setVersion} options={selected.versions.map(item => ({ value: item.version!, label: `${item.version} · ${item.name}` }))} />
          {release ? <><p>{release.description}</p><SystemConfigPreview document={release} /><Button type="primary" disabled={locked || dirty} onClick={() => use(release, true)}>使用此版本问答</Button></> : null}
        </Card> : null}
      </>}
    </div></div>
    <Modal title="切换协作流程" open={Boolean(pendingWorkflow)} onCancel={() => setPendingWorkflow(undefined)}
      footer={<div className="system-actions">
        <Button onClick={() => setPendingWorkflow(undefined)}>取消</Button>
        <Button disabled={locked} onClick={() => pendingWorkflow && applyWorkflow(pendingWorkflow, false)}>保留现有实例</Button>
        <Button type="primary" disabled={locked} onClick={() => pendingWorkflow && applyWorkflow(pendingWorkflow, true)}>使用新流程默认配置</Button>
      </div>}>
      <p>当前实例已有保存或自定义配置。使用新流程默认配置将替换当前编辑区的全部实例，包括提示词、模型参数和知识源绑定。</p>
      <p>选择保留现有实例后，可以根据新流程的角色要求继续调整。更改在保存草稿后生效，已发布版本保持不变。</p>
    </Modal>
  </main>
}
