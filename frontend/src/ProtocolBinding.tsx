import { useEffect, useState } from 'react'
import { Alert, Button, Select, Tooltip } from 'antd'
import { ReloadOutlined, WorkflowOutlined } from './Icons'
import { protocolRequest } from './api'
import type { ManagedProtocol, ProtocolDocument } from './protocolTypes'
import type { SystemDraftFields } from './types'

// Pins a system draft to one explicit protocol revision or published version.
const key = (d: ProtocolDocument) => `${d.protocol_id}@${d.version ?? `r${d.revision}`}`
export default function ProtocolBinding({ fields, frozen, disabled, active = true, onChange, onSelect, onManage }: {
  fields: SystemDraftFields; frozen?: ProtocolDocument; disabled: boolean; active?: boolean
  onChange: (fields: SystemDraftFields) => void; onSelect: (doc: ProtocolDocument) => void; onManage?: () => void
}) {
  const [items, setItems] = useState<ManagedProtocol[]>([]), [error, setError] = useState(''), [loaded, setLoaded] = useState(false)
  const [loading, setLoading] = useState(false)
  async function reload() {
    setLoading(true)
    try { const r = await protocolRequest<{ items: ManagedProtocol[] }>(''); setItems(r.items); setError(''); setLoaded(true) }
    catch (e) { setError(String(e)) }
    finally { setLoading(false) }
  }
  useEffect(() => { if (active) void reload() }, [active])
  const published = items.filter(p => !p.archived).flatMap(p => p.versions), drafts = items.filter(p => !p.archived).map(p => p.draft)
  const choices = [...published, ...drafts]
  const selectedKey = fields.protocol_ref ? `${fields.protocol_ref.protocol_id}@${fields.protocol_ref.version ?? `r${fields.protocol_ref.revision}`}` : undefined
  const selected = frozen && key(frozen) === selectedKey ? frozen : choices.find(d => key(d) === selectedKey)
  const option = (d: ProtocolDocument) => ({ value: key(d), label: `${d.name} · ${d.version ?? `草稿 r${d.revision}`}` })
  return <section className="protocol-binding" aria-label="协议绑定">
    {error && <Alert type="warning" title="协议列表加载失败；保留当前明确引用" description={error} />}
    <div className="protocol-picker-row">
    <Select aria-label="执行协议" disabled={disabled} value={selectedKey} placeholder="选择个人协议的明确版本或已保存修订"
      options={[{ label: '我的已发布协议', options: published.map(option) }, { label: '我的协议草稿', options: drafts.map(option) },
        ...(selected && !choices.some(d => key(d) === selectedKey) ? [{ label: '当前固定引用', options: [option(selected)] }] : [])]}
      onChange={v => { const doc = choices.find(d => key(d) === v); if (doc && key(doc) !== selectedKey) onSelect(doc) }} />
    <Tooltip title="刷新协议列表"><Button className="protocol-icon-button" aria-label="刷新协议列表" disabled={disabled || loading}
      loading={loading} icon={<ReloadOutlined />} onClick={() => void reload()} /></Tooltip>
    {onManage && <Tooltip title="创建或管理协作协议"><Button className="protocol-icon-button" aria-label="创建或管理协作协议"
      disabled={disabled} icon={<WorkflowOutlined />} onClick={onManage} /></Tooltip>}
    </div>
    {loaded && !choices.length && !selected && <p className="ws-hint">暂无个人协议。先到协作协议页面创建并保存，再返回选择。</p>}
    {selected && <><p className="ws-hint">固定引用：{selected.name} · {selected.version ?? `草稿 r${selected.revision}`}。草稿可试运行；系统发布须绑定协议发布版本。</p>
      <div className="ws-binding-grid">
        {Object.entries(selected.spec.slots).map(([slot, c]) => <label className="ws-field" key={slot}>
          <span>{slot} · {c.role} · {c.minimum}～{c.maximum} 个</span>
          <Select aria-label={`${slot} 实例绑定`} mode="multiple" disabled={disabled} value={fields.protocol_bindings?.[slot] ?? []}
            options={(fields.agents ?? []).filter(a => a.agent_type === c.role).map(a => ({ value: a.agent_id, label: `${a.name} · ${a.agent_id}` }))}
            onChange={ids => onChange({ ...fields, protocol_bindings: { ...fields.protocol_bindings, [slot]: ids } })} />
        </label>)}
      </div></>}
  </section>
}
