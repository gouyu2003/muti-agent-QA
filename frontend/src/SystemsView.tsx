import { useEffect, useState } from 'react'
import { Alert, Button, Card, Collapse, Empty, Input, Modal, Select, Tag } from 'antd'
import { getAgentTemplates, getManagedSystem, getManagedSystems, publishManagedSystem, saveManagedSystem } from './api'
import { AgentEditor, newInstance } from './AgentEditor'
import { PromptBlock } from './PromptBlock'
import ProtocolBinding from './ProtocolBinding'
import { ProtocolPreview } from './ProtocolsView'
import { configurationGaps, protocolDefaults, retainedBindings } from './systemProtocol'
import type { ProtocolDocument } from './protocolTypes'
import type { AgentInstance, AgentTemplate, ManagedSelection, ManagedSystem, SystemDocument, SystemDraftFields, SystemOptions } from './types'

function fieldsOf(document: SystemDocument): SystemDraftFields {
  const { name, description, workflow_id, source_id, source_version } = document
  const agents = document.agents ?? document.snapshot.instances ?? Object.entries(document.snapshot.agents).map(([id, a]) => ({
    agent_id: id, agent_type: a.role as AgentInstance['agent_type'], name: a.agent_name, description: a.description ?? '',
    model_config: a.role === 'retriever' ? null : a.model_config,
    source_id: a.role === 'retriever' ? document.source_id : null,
    source_version: a.role === 'retriever' ? document.source_version : null, top_k: a.retrieval_config?.top_k ?? 3,
  }))
  return { name, description, workflow_id, source_id, source_version, agents, execution_mode: document.execution_mode ?? 'sequential',
    ...(document.protocol_ref ? { protocol_ref: document.protocol_ref, protocol_bindings: document.protocol_bindings } : {}) }
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
    {document.snapshot.protocol_document && <ProtocolPreview spec={document.snapshot.protocol_document.spec} />}
  </div>
}

export default function SystemsView({ options, disabled, onUse, onHistory, active = true, onManageProtocols }: {
  options?: SystemOptions; disabled: boolean; active?: boolean; onManageProtocols?: () => void; onUse: (selection: ManagedSelection) => void; onHistory: (id: string) => void
}) {
  const empty: SystemDraftFields = { name: '', description: '', workflow_id: null, source_id: null, source_version: null, agents: [], protocol_ref: null, protocol_bindings: {} }
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
  const [pendingProtocol, setPendingProtocol] = useState<ProtocolDocument>()
  const [protocol, setProtocol] = useState<ProtocolDocument>()
  const dirty = selected ? JSON.stringify(fields) !== JSON.stringify(fieldsOf(selected.draft)) : Boolean(fields.name || fields.description || fields.source_id || (editing && fields.agents?.length))
  const release = selected?.versions.find(item => item.version === version)
  const issues = selected?.draft.snapshot.configuration_errors ?? []
  const currentGaps = configurationGaps(fields, protocol?.spec, options)
  const ready = Boolean(selected && !dirty && !currentGaps.length && fields.workflow_id &&
    (['2', '3'].includes(selected.draft.snapshot.schema_version ?? '') ? !issues.length && selected.draft.snapshot.workflow_spec : selected.draft.snapshot.source))

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    getManagedSystems(controller.signal).then(data => setSystems(data.items)).catch((reason: unknown) => {
      if (!(reason instanceof Error && reason.name === 'AbortError')) setError(String(reason))
    })
    getAgentTemplates(controller.signal).then(data => setTemplates(data.items)).catch((reason: unknown) => {
      if (!(reason instanceof Error && reason.name === 'AbortError')) setTemplateError('智能体模板加载失败，请刷新后重试。')
    })
    return () => controller.abort()
  }, [active])

  function adopt(system?: ManagedSystem) {
    if (dirty && !window.confirm('草稿有未保存的修改，确定放弃这些修改吗？')) return
    let next: SystemDraftFields
    try {
      next = system ? fieldsOf(system.draft) : structuredClone(empty)
    } catch (reason) { setError((reason as Error).message); return }
    setSelected(system); setFields(next); setProtocol(system?.draft.snapshot.protocol_document)
    setGeneratedAgents(system ? undefined : JSON.stringify(next.agents)); setPendingProtocol(undefined)
    setVersion(system?.versions[0]?.version); setEditing(true); setError(''); setNotice('')
  }

  function applyProtocol(doc: ProtocolDocument, replace: boolean) {
    try {
      const defaults = replace ? protocolDefaults(doc.spec, templates) : undefined
      const next = { ...fields, workflow_id: doc.protocol_id,
        protocol_ref: { protocol_id: doc.protocol_id, ...(doc.version ? { version: doc.version } : { revision: doc.revision }) },
        agents: defaults?.agents ?? fields.agents,
        protocol_bindings: defaults?.bindings ?? retainedBindings(doc.spec, fields),
        source_id: null, source_version: null }
      setFields(next); setProtocol(structuredClone(doc))
      if (replace) setGeneratedAgents(JSON.stringify(next.agents))
      setPendingProtocol(undefined); setError(''); setNotice('')
    } catch (reason) { setError((reason as Error).message) }
  }

  function changeProtocol(doc: ProtocolDocument) {
    if (!fields.agents?.length || (!selected && JSON.stringify(fields.agents) === generatedAgents)) applyProtocol(doc, true)
    else setPendingProtocol(doc)
  }

  async function action(work: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await work() } catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败，请重试。') }
    finally { setBusy(false) }
  }

  async function save() {
    await action(async () => {
      const saved = await saveManagedSystem(fields, selected?.system_id, selected?.draft_revision)
      setSelected(saved); setFields(fieldsOf(saved.draft)); setProtocol(saved.draft.snapshot.protocol_document); setGeneratedAgents(undefined); setSystems((await getManagedSystems()).items)
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
    <header className="history-hero"><div><span className="section-kicker">MY QA SYSTEMS</span><h1>我的系统</h1><p>先选择已保存的个人协议，再配置实例与知识源，保存草稿或发布版本。</p></div>
      <Button disabled={locked || !templates.length} onClick={() => adopt()}>新建系统</Button></header>
    {templateError ? <Alert type="error" title={templateError} /> : null}
    {error ? <Alert type="error" title="系统操作未完成" description={error} action={selected ? <Button onClick={() => void action(async () => {
      const current = await getManagedSystem(selected.system_id); setSelected(current); setFields(fieldsOf(current.draft)); setProtocol(current.draft.snapshot.protocol_document); setNotice('已重新读取服务器草稿。')
      setGeneratedAgents(undefined); setPendingProtocol(undefined)
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
          </div>
          {!fields.protocol_ref && selected && fields.workflow_id && <p>当前保存配置使用兼容流程：{selected.draft.snapshot.protocol.protocol_name ?? fields.workflow_id}。可继续试运行；选择个人协议后可调整协作方式。</p>}
          <ProtocolBinding fields={fields} frozen={protocol} disabled={locked || !templates.length} active={active}
            onChange={setFields} onSelect={changeProtocol} onManage={onManageProtocols} />
          <h3>当前编辑预览</h3>
          {protocol ? <><p>角色要求：{Object.entries(protocol.spec.slots).map(([slot, c]) => `${slot} · ${c.role} ${c.minimum}～${c.maximum} 个`).join('；')}</p><ProtocolPreview spec={protocol.spec} /></> : <p>选择执行协议后生成默认实例，并显示协议流程。</p>}
          <p>实例材料由你指定。协议执行方式由各阶段配置决定。</p>
          <AgentEditor agents={fields.agents ?? []} templates={templates} options={options} disabled={locked}
            reusableAgents={systems.filter(s => s.system_id !== selected?.system_id).flatMap(s => (fieldsOf(s.draft).agents ?? []).map(a => ({
              key: `${s.system_id}:${a.agent_id}`, label: `${s.draft.name} · ${a.name}`, agent: a,
            })))}
            onChange={agents => setFields({ ...fields, agents, source_id: null, source_version: null })} />
          {!dirty && issues.length ? <Alert type="warning" title="草稿已保存，仍需补齐配置" description={issues.join('；')} /> : null}
          {(dirty || !issues.length) && currentGaps.length ? <Alert type="warning" title="当前配置仍需补齐"
            description={<ul>{currentGaps.map(gap => <li key={gap}>{gap}</li>)}</ul>} /> : null}
          <div className="system-actions"><Button type="primary" disabled={locked || !fields.name.trim() || (!selected && !fields.protocol_ref)} onClick={() => void save()}>{selected ? '保存草稿' : '创建草稿'}</Button>
            <Button disabled={locked || !ready} onClick={() => selected && use(selected.draft, false)}>试运行已保存草稿</Button>
            <Button disabled={locked || !ready || !!(fields.protocol_ref && !fields.protocol_ref.version)} onClick={() => void action(async () => {
              if (!selected) return
              const published = await publishManagedSystem(selected.system_id, selected.draft_revision)
              const current = await getManagedSystem(selected.system_id)
              setSelected(current); setVersion(published.version); setSystems((await getManagedSystems()).items)
              setNotice(`已发布 ${published.version}，此版本配置不可覆盖。`)
            })}>发布版本</Button>
            {selected ? <Button disabled={locked} onClick={() => onHistory(selected.system_id)}>查看此系统历史</Button> : null}
          </div>
          {dirty ? <p>先保存修改，再试运行或发布。下方已保存草稿为上次保存的配置。</p> : null}
          {selected ? <><h3>已保存草稿 r{selected.draft_revision}</h3><SystemConfigPreview document={selected.draft} /></> : null}
        </Card>
        {selected?.versions.length ? <Card title="已发布版本（只读）">
          <Select aria-label="发布版本" value={version} onChange={setVersion} options={selected.versions.map(item => ({ value: item.version!, label: `${item.version} · ${item.name}` }))} />
          {release ? <><p>{release.description}</p><SystemConfigPreview document={release} /><Button type="primary" disabled={locked || dirty} onClick={() => use(release, true)}>使用此版本问答</Button></> : null}
        </Card> : null}
      </>}
    </div></div>
    <Modal title="切换执行协议" open={Boolean(pendingProtocol)} onCancel={() => setPendingProtocol(undefined)}
      footer={<div className="system-actions">
        <Button onClick={() => setPendingProtocol(undefined)}>取消</Button>
        <Button disabled={locked} onClick={() => pendingProtocol && applyProtocol(pendingProtocol, false)}>保留现有实例</Button>
        <Button type="primary" disabled={locked} onClick={() => pendingProtocol && applyProtocol(pendingProtocol, true)}>生成新协议默认实例</Button>
      </div>}>
      <p>当前实例已有保存或自定义配置。生成默认实例将替换当前编辑区的全部实例，使用新 ID，材料需要重新配置。</p>
      <p>保留实例会保留 ID、提示词、模型和材料，仅保留仍合法的绑定；有歧义的槽位需手动选择。保存后生效。</p>
    </Modal>
  </main>
}
