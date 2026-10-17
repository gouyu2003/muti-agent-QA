import { useEffect, useState } from 'react'
import { Alert, Button, Card, Collapse, Empty, Input, Select, Tag } from 'antd'
import { getManagedSystem, getManagedSystems, publishManagedSystem, saveManagedSystem } from './api'
import type { ManagedSelection, ManagedSystem, SystemDocument, SystemDraftFields, SystemOptions } from './types'

function fieldsOf(document: SystemDraftFields): SystemDraftFields {
  const { name, description, workflow_id, source_id, source_version } = document
  return { name, description, workflow_id, source_id, source_version }
}

export function SystemConfigPreview({ document }: { document: SystemDocument }) {
  return <div className="system-config-preview">
    <p>流程：{document.workflow_id ? document.snapshot.protocol.protocol_name ?? document.workflow_id : '尚未选择'} · 知识源：{document.snapshot.source?.name ?? '尚未选择'}{document.source_version ? ` · 版本 ${document.source_version}` : ''}</p>
    <Collapse items={Object.entries(document.snapshot.agents).map(([id, agent]) => ({ key: id,
      label: `${agent.agent_name} · ${agent.model_config.model_name}（只读）`,
      children: <><p>温度 {agent.model_config.temperature} · Token 上限 {agent.model_config.max_tokens}</p><p className="system-prompt">{agent.model_config.system_prompt}</p></>,
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
  const [editing, setEditing] = useState(false)
  const dirty = selected ? JSON.stringify(fields) !== JSON.stringify(fieldsOf(selected.draft)) : Boolean(fields.name || fields.description || fields.source_id)
  const source = options?.sources.find(item => item.source_id === fields.source_id)
  const release = selected?.versions.find(item => item.version === version)

  useEffect(() => {
    const controller = new AbortController()
    getManagedSystems(controller.signal).then(data => setSystems(data.items)).catch((reason: unknown) => {
      if (!(reason instanceof Error && reason.name === 'AbortError')) setError(String(reason))
    })
    return () => controller.abort()
  }, [])

  function adopt(system?: ManagedSystem) {
    if (dirty && !window.confirm('草稿有未保存的修改，确定放弃这些修改吗？')) return
    setSelected(system); setFields(system ? fieldsOf(system.draft) : empty)
    setVersion(system?.versions[0]?.version); setEditing(true); setError(''); setNotice('')
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
      <Button disabled={locked} onClick={() => adopt()}>新建系统</Button></header>
    {error ? <Alert type="error" title="系统操作未完成" description={error} action={selected ? <Button onClick={() => void action(async () => {
      const current = await getManagedSystem(selected.system_id); setSelected(current); setFields(fieldsOf(current.draft)); setNotice('已重新读取服务器草稿。')
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
            <label htmlFor="system-workflow">协作流程</label><Select id="system-workflow" value={fields.workflow_id} allowClear disabled={locked} placeholder="选择现有流程" options={options?.workflows.map(item => ({ value: item.workflow_id, label: item.name }))} onChange={value => setFields({ ...fields, workflow_id: value ?? null })} />
            <label htmlFor="system-source">知识源</label><Select id="system-source" value={fields.source_id} allowClear disabled={locked} placeholder="选择工作区共享材料" options={options?.sources.map(item => ({ value: item.source_id, label: item.name }))} onChange={value => setFields({ ...fields, source_id: value ?? null, source_version: null })} />
            {source?.source_type === 'document_corpus' ? <><label htmlFor="system-source-version">固定知识源版本</label><Select id="system-source-version" value={fields.source_version} disabled={locked} placeholder="选择具体版本" options={source.versions?.map(value => ({ value, label: `版本 ${value}` }))} onChange={value => setFields({ ...fields, source_version: value })} /></> : null}
          </div>
          <div className="system-actions"><Button type="primary" disabled={locked || !fields.name.trim()} onClick={() => void save()}>{selected ? '保存草稿' : '创建草稿'}</Button>
            <Button disabled={locked || !selected || dirty || !fields.workflow_id || !fields.source_id || !selected.draft.snapshot.source} onClick={() => selected && use(selected.draft, false)}>试运行已保存草稿</Button>
            <Button disabled={locked || !selected || dirty || !fields.workflow_id || !fields.source_id || !selected.draft.snapshot.source} onClick={() => void action(async () => {
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
  </main>
}
