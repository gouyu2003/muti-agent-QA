import { useEffect, useState } from 'react'
import { Alert, Button, Collapse, Input, Modal, Select, Tag, Tooltip } from 'antd'
import { deleteManagedSystem, getAgentTemplates, getManagedSystem, getManagedSystems, publishManagedSystem, saveManagedSystem } from './api'
import { DeleteOutlined, SearchOutlined } from './Icons'
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

export default function SystemsView({ options, disabled, onUse, onHistory, active = true, onManageProtocols, onDeleted }: {
  options?: SystemOptions; disabled: boolean; active?: boolean; onManageProtocols?: () => void; onUse: (selection: ManagedSelection) => void; onHistory: (id: string) => void; onDeleted?: (id: string) => void
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
  const [deleteTarget, setDeleteTarget] = useState<ManagedSystem>()
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const dirty = selected ? JSON.stringify(fields) !== JSON.stringify(fieldsOf(selected.draft)) : Boolean(fields.name || fields.description || fields.source_id || (editing && fields.agents?.length))
  const release = selected?.versions.find(item => item.version === version)
  const issues = selected?.draft.snapshot.configuration_errors ?? []
  const currentGaps = configurationGaps(fields, protocol?.spec, options)
  const ready = Boolean(selected && !dirty && !currentGaps.length && fields.workflow_id &&
    (['2', '3'].includes(selected.draft.snapshot.schema_version ?? '') ? !issues.length && selected.draft.snapshot.workflow_spec : selected.draft.snapshot.source))

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    getManagedSystems(controller.signal).then(data => { setSystems(data.items); setLoading(false) }).catch((reason: unknown) => {
      if (!(reason instanceof Error && reason.name === 'AbortError')) { setError(String(reason)); setLoading(false) }
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

  async function remove() {
    if (!deleteTarget || disabled || busy) return
    const target = deleteTarget
    await action(async () => {
      await deleteManagedSystem(target.system_id, target.draft_revision)
      setSystems(current => current.filter(item => item.system_id !== target.system_id))
      setSelected(undefined); setFields(structuredClone(empty)); setVersion(undefined); setEditing(false)
      setProtocol(undefined); setPendingProtocol(undefined); setGeneratedAgents(undefined); setDeleteTarget(undefined)
      setNotice(`已删除「${target.draft.name}」。已有运行历史仍保留。`)
      onDeleted?.(target.system_id)
    })
  }

  function use(document: SystemDocument, published: boolean) {
    if (!selected) return
    onUse({ systemId: selected.system_id, name: document.name, document,
      ...(published ? { systemVersion: document.version } : { draftRevision: selected.draft_revision }) })
  }

  const locked = disabled || busy
  const term = query.trim().toLowerCase()
  const visibleSystems = term
    ? systems.filter(item => item.draft.name.toLowerCase().includes(term) || (item.draft.description ?? '').toLowerCase().includes(term))
    : systems
  const publishHint = dirty ? '先保存修改再发布版本。'
    : fields.protocol_ref && !fields.protocol_ref.version ? '需先绑定协议的发布版本。'
    : currentGaps.length ? '还需补齐配置项。'
    : ready ? undefined : '配置尚未完成。'
  const runHint = dirty ? '先保存修改再试运行。' : currentGaps.length ? '还需补齐配置项。' : ready ? undefined : '配置尚未完成。'
  const saveHint = !fields.name.trim() ? '请填写系统名称。' : !selected && !fields.protocol_ref ? '请先选择执行协议。' : undefined
  const footerNote = dirty ? '修改尚未保存；试运行与发布使用上一次保存的配置。'
    : ready ? '配置完整，可试运行或发布。'
    : currentGaps.length ? `还有 ${currentGaps.length} 项配置待补齐。`
    : '保存草稿后可以试运行或发布。'

  return <main className="systems-workspace">
    <header className="ws-page-head">
      <div className="ws-page-title">
        <span className="section-kicker">MY QA SYSTEMS</span>
        <h1>我的系统</h1>
        <p>先选择已保存的个人协议，再配置实例与知识源，保存草稿或发布版本。</p>
      </div>
      <div className="ws-page-actions">
        <Button type="primary" disabled={locked || !templates.length} onClick={() => adopt()}>新建系统</Button>
      </div>
    </header>

    {(templateError || error || notice || (editing && !dirty && issues.length) || (editing && (dirty || !issues.length) && currentGaps.length)) ? <div className="ws-notices">
      {templateError ? <Alert type="error" title={templateError} /> : null}
      {error ? <Alert type="error" title="系统操作未完成" description={error} action={selected ? <Button onClick={() => void action(async () => {
        const current = await getManagedSystem(selected.system_id); setSelected(current); setFields(fieldsOf(current.draft)); setProtocol(current.draft.snapshot.protocol_document); setNotice('已重新读取服务器草稿。')
        setGeneratedAgents(undefined); setPendingProtocol(undefined)
      })}>重新读取草稿</Button> : undefined} /> : null}
      {notice ? <Alert type="success" title={notice} /> : null}
      {editing && !dirty && issues.length ? <Alert type="warning" title="草稿已保存，仍需补齐配置" description={issues.join('；')} /> : null}
      {editing && (dirty || !issues.length) && currentGaps.length ? <Alert type="warning" title="当前配置仍需补齐"
        description={<ul>{currentGaps.map(gap => <li key={gap}>{gap}</li>)}</ul>} /> : null}
    </div> : null}

    <div className="ws-layout">
      <aside className="ws-rail" aria-label="我的系统列表">
        <div className="ws-rail-head">
          <h2>系统 <span className="ws-count">{systems.length}</span></h2>
          <Input aria-label="搜索系统" placeholder="搜索系统名称" allowClear prefix={<SearchOutlined />}
            value={query} onChange={event => setQuery(event.target.value)} />
        </div>
        {visibleSystems.length ? <div className="ws-rail-list">
          {visibleSystems.map(item => <button key={item.system_id} type="button" disabled={locked}
            aria-current={selected?.system_id === item.system_id ? 'true' : undefined}
            className={`ws-rail-item${selected?.system_id === item.system_id ? ' is-active' : ''}`} onClick={() => adopt(item)}>
            <span className="ws-rail-copy">
              <strong>{item.draft.name}</strong>
              <small>草稿 r{item.draft_revision} · {item.versions[0]?.version ?? '未发布'}</small>
            </span>
            <span className={`ws-rail-flag${item.versions.length ? ' is-live' : ''}`}>{item.versions.length ? '已发布' : '草稿'}</span>
          </button>)}
        </div> : <p className="ws-rail-empty">{term ? '没有匹配的系统。' : loading ? '正在加载系统…' : '尚未创建系统'}</p>}
      </aside>

      <div className="ws-canvas">
        {!editing ? <div className="ws-empty">
          <strong>尚未选择系统</strong>
          <p>选择已有系统，或点击新建系统。</p>
        </div> : <>
          <div className="ws-sticky-shell">
          <section className="ws-toolbar" aria-label="系统编辑工具条">
            <div className="ws-toolbar-main">
              <span className="ws-overline">{selected ? '编辑系统草稿' : '创建系统草稿'}</span>
              <h2>{fields.name.trim() || '未命名系统'}</h2>
              <div className="ws-badges">
                {dirty ? <Tag color="orange">有未保存修改</Tag> : selected ? <Tag>已保存</Tag> : null}
                {selected ? <Tag>草稿 r{selected.draft_revision}</Tag> : null}
              </div>
            </div>
            <div className="ws-toolbar-actions">
              {selected && <Button danger icon={<DeleteOutlined />} disabled={locked} onClick={() => { setError(''); setDeleteTarget(selected) }}>删除系统</Button>}
            </div>
          </section>
          </div>

          <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">01</span><h3>基础信息</h3></header>
            <div className="ws-section-body">
              <div className="ws-field-row">
                <label className="ws-field" htmlFor="system-name"><span>系统名称</span>
                  <Input id="system-name" value={fields.name} maxLength={100} disabled={locked} onChange={event => setFields({ ...fields, name: event.target.value })} /></label>
                <label className="ws-field" htmlFor="system-description"><span>系统说明</span>
                  <Input.TextArea id="system-description" value={fields.description} maxLength={2000} disabled={locked} onChange={event => setFields({ ...fields, description: event.target.value })} /></label>
              </div>
            </div>
          </section>

          <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">02</span><h3>执行协议</h3>
              <div className="ws-section-extra">{fields.protocol_ref ? <Tag color="blue">已固定引用</Tag> : <Tag color="orange">未绑定</Tag>}</div>
            </header>
            <div className="ws-section-body">
              {!fields.protocol_ref && selected && fields.workflow_id && <p className="ws-hint">当前保存配置使用兼容流程：{selected.draft.snapshot.protocol.protocol_name ?? fields.workflow_id}。可继续试运行；选择个人协议后可调整协作方式。</p>}
              <ProtocolBinding fields={fields} frozen={protocol} disabled={locked || !templates.length} active={active}
                onChange={setFields} onSelect={changeProtocol} onManage={onManageProtocols} />
            </div>
          </section>

          <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">03</span><h3>智能体实例</h3></header>
            <div className="ws-section-body">
              <AgentEditor agents={fields.agents ?? []} templates={templates} options={options} disabled={locked}
                reusableAgents={systems.filter(s => s.system_id !== selected?.system_id).flatMap(s => (fieldsOf(s.draft).agents ?? []).map(a => ({
                  key: `${s.system_id}:${a.agent_id}`, label: `${s.draft.name} · ${a.name}`, agent: a,
                })))}
                onChange={agents => setFields({ ...fields, agents, source_id: null, source_version: null })} />
            </div>
          </section>

          <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">04</span><h3>当前编辑预览</h3>
              {protocol ? <div className="ws-section-extra"><Tag>{protocol.name}</Tag></div> : null}
            </header>
            <div className="ws-section-body">
              {protocol ? <>
                <p className="ws-hint">角色要求：{Object.entries(protocol.spec.slots).map(([slot, c]) => `${slot} · ${c.role} ${c.minimum}～${c.maximum} 个`).join('；')}</p>
                <ProtocolPreview spec={protocol.spec} />
              </> : <p className="ws-hint">选择执行协议后生成默认实例，并显示协议流程。</p>}
              <p className="ws-hint">实例材料由你指定。协议执行方式由各阶段配置决定。</p>
            </div>
          </section>

          {selected ? <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">05</span><h3>已保存草稿 r{selected.draft_revision}</h3></header>
            <div className="ws-section-body"><SystemConfigPreview document={selected.draft} /></div>
          </section> : null}

          {selected?.versions.length ? <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">06</span><h3>已发布版本（只读）</h3></header>
            <div className="ws-section-body">
              <label className="ws-field ws-field-inline"><span>发布版本</span>
                <Select aria-label="发布版本" value={version} onChange={setVersion} options={selected.versions.map(item => ({ value: item.version!, label: `${item.version} · ${item.name}` }))} />
              </label>
              {release ? <>
                {release.description ? <p className="ws-hint">{release.description}</p> : null}
                <SystemConfigPreview document={release} />
                <div className="ws-row-actions">
                  <Button type="primary" disabled={locked || dirty} onClick={() => use(release, true)}>使用此版本问答</Button>
                </div>
              </> : <p className="ws-hint">选择一个发布版本查看只读配置。</p>}
            </div>
          </section> : null}

          <footer className="ws-action-bar">
            <p className="ws-action-note">{footerNote}</p>
            <div className="ws-action-buttons">
              {selected ? <Button disabled={locked} onClick={() => onHistory(selected.system_id)}>查看此系统历史</Button> : null}
              <Tooltip title={runHint}><span className="ws-btn-wrap">
                <Button disabled={locked || !ready} onClick={() => selected && use(selected.draft, false)}>试运行已保存草稿</Button>
              </span></Tooltip>
              <Tooltip title={publishHint}><span className="ws-btn-wrap">
                <Button disabled={locked || !ready || !!(fields.protocol_ref && !fields.protocol_ref.version)} onClick={() => void action(async () => {
                  if (!selected) return
                  const published = await publishManagedSystem(selected.system_id, selected.draft_revision)
                  const current = await getManagedSystem(selected.system_id)
                  setSelected(current); setVersion(published.version); setSystems((await getManagedSystems()).items)
                  setNotice(`已发布 ${published.version}，此版本配置不可覆盖。`)
                })}>发布版本</Button>
              </span></Tooltip>
              <Tooltip title={saveHint}><span className="ws-btn-wrap">
                <Button type="primary" disabled={locked || !fields.name.trim() || (!selected && !fields.protocol_ref)} onClick={() => void save()}>{selected ? '保存草稿' : '创建草稿'}</Button>
              </span></Tooltip>
            </div>
          </footer>
        </>}
      </div>
    </div>

    <Modal title="删除系统" open={Boolean(deleteTarget)} okText="确认删除" cancelText="取消"
      okButtonProps={{ danger: true, disabled }} cancelButtonProps={{ disabled: busy }} confirmLoading={busy}
      closable={!busy} mask={{ closable: !busy }} keyboard={!busy}
      onCancel={() => { if (!busy) setDeleteTarget(undefined) }} onOk={() => void remove()}>
      <p>确定删除「{deleteTarget?.draft.name}」吗？系统草稿及全部发布版本将被删除，已有运行历史仍会保留。</p>
      {dirty && <p>当前未保存的修改也会丢弃。</p>}
      {error && <Alert type="error" title="删除失败" description={error} />}
    </Modal>
    <Modal title="切换执行协议" open={Boolean(pendingProtocol)} onCancel={() => setPendingProtocol(undefined)}
      footer={<div className="ws-modal-actions">
        <Button onClick={() => setPendingProtocol(undefined)}>取消</Button>
        <Button disabled={locked} onClick={() => pendingProtocol && applyProtocol(pendingProtocol, false)}>保留现有实例</Button>
        <Button type="primary" disabled={locked} onClick={() => pendingProtocol && applyProtocol(pendingProtocol, true)}>生成新协议默认实例</Button>
      </div>}>
      <p>当前实例已有保存或自定义配置。生成默认实例将替换当前编辑区的全部实例，使用新 ID，材料需要重新配置。</p>
      <p>保留实例会保留 ID、提示词、模型和材料，仅保留仍合法的绑定；有歧义的槽位需手动选择。保存后生效。</p>
    </Modal>
  </main>
}
