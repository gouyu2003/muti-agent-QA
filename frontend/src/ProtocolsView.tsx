import { useEffect, useState } from 'react'
import { Alert, Button, Card, Checkbox, Collapse, Input, InputNumber, Select, Space } from 'antd'
import { protocolRequest } from './api'
import { togglePlanner } from './protocolEditing'
import type { ManagedProtocol, ProtocolDocument, ProtocolCatalog, ProtocolSpec } from './protocolTypes'

export function ProtocolPreview({ spec }: { spec: ProtocolSpec }) {
  return <ol aria-label="协议流程预览">{spec.stages.map(s => <li key={s.stage_id}>
    <strong>{s.stage_id}</strong> · {s.kind} · {s.slot} · {s.mode === 'parallel' ? '并行后汇合' : '顺序'} · 最多 {s.max_visits} 次
    <div>读取：{s.reads.join('、') || '独立输入'}；{s.routes.map(r => `${r.condition} → ${r.target}`).join('；')}</div>
  </li>)}</ol>
}

export default function ProtocolsView({ disabled, active = true, onReturnToSystems }: { disabled: boolean; active?: boolean; onReturnToSystems?: () => void }) {
  const [items, setItems] = useState<ManagedProtocol[]>([])
  const [catalog, setCatalog] = useState<ProtocolCatalog>({ items: [], capabilities: {} })
  const templates = catalog.items
  const [selected, setSelected] = useState<ManagedProtocol>()
  const [fields, setFields] = useState<{ name: string; description: string; spec: ProtocolSpec }>()
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false)
  const [release, setRelease] = useState<ProtocolDocument>()
  const dirty = !!fields && (!selected || JSON.stringify(fields) !== JSON.stringify({ name: selected.draft.name, description: selected.draft.description, spec: selected.draft.spec }))
  const capability = fields ? catalog.capabilities?.[fields.spec.language_version] : undefined
  const locked = disabled || busy || !!selected?.archived || (!!fields && !capability)
  async function reload() { setItems((await protocolRequest<{ items: ManagedProtocol[] }>('')).items) }
  useEffect(() => {
    if (!active) return
    let disposed = false
    void Promise.all([reload(), protocolRequest<ProtocolCatalog>('/templates').then(r => {
      if (!disposed) { setCatalog(r); if (!r.capabilities) setError('协议编辑能力加载失败，请刷新。') }
    })]).catch(e => { if (!disposed) setError(String(e)) })
    return () => { disposed = true }
  }, [active])
  async function act(work: () => Promise<void>) { setBusy(true); setError(''); setNotice(''); try { await work() } catch (e) { setError((e as Error).message) } finally { setBusy(false) } }
  function adopt(item?: ManagedProtocol, spec?: ProtocolSpec) {
    if (dirty && !window.confirm('协议有未保存修改，确定放弃吗？')) return
    setSelected(item); setFields(item ? { name: item.draft.name, description: item.draft.description, spec: structuredClone(item.draft.spec) }
      : spec ? { name: '我的协作协议', description: '', spec: structuredClone(spec) } : undefined)
    setRelease(item?.versions[0]); setError(''); setNotice('')
  }
  function specPatch(patch: Partial<ProtocolSpec>) { if (fields) setFields({ ...fields, spec: { ...fields.spec, ...patch } }) }
  function stagePatch(index: number, patch: Partial<ProtocolSpec['stages'][number]>) { if (fields) specPatch({ stages: fields.spec.stages.map((s, i) => i === index ? { ...s, ...patch } : s) }) }
  return <main className="systems-workspace"><h1>我的协作协议</h1><p>定义阶段、参与角色、共享范围和路由，再绑定到我的系统。所有执行规则由服务端校验。</p>
    {error && <Alert type="error" title="协议操作未完成" description={error} />}{notice && <Alert type="success" title={notice} />}
    {onReturnToSystems && <Button disabled={disabled || busy} onClick={onReturnToSystems}>返回我的系统</Button>}
    <div className="protocol-template-grid">{templates.map(t => <Card key={t.template_id} size="small" title={t.name}>
      <p>{t.description}</p><p>{Object.entries(t.spec.slots).map(([slot, c]) => `${slot}：${c.minimum}～${c.maximum}`).join(' · ')}</p>
      <Button disabled={disabled || busy || !catalog.capabilities?.[t.spec.language_version]} onClick={() => adopt(undefined, t.spec)}>从 {t.name} 创建</Button>
    </Card>)}</div>
    <div className="systems-grid"><aside className="systems-list" aria-label="协议列表">{items.map(p => <button key={p.protocol_id} disabled={disabled || busy} onClick={() => adopt(p)}>
      <strong>{p.draft.name}</strong><span>r{p.draft_revision} · {p.versions[0]?.version ?? '未发布'}{p.archived ? ' · 已归档' : ''}</span>
    </button>)}</aside><div className="systems-editor">{fields && <Card title={dirty ? '编辑协议 · 有未保存修改' : '已保存协议'}>
      <label>协议名称<Input aria-label="协议名称" disabled={locked} value={fields.name} maxLength={100} onChange={e => setFields({ ...fields, name: e.target.value })} /></label>
      <label>协议说明<Input.TextArea aria-label="协议说明" disabled={locked} value={fields.description} maxLength={2000} onChange={e => setFields({ ...fields, description: e.target.value })} /></label>
      <label>入口阶段<Select aria-label="入口阶段" disabled={locked} value={fields.spec.entry_stage} options={fields.spec.stages.map(s => ({ value: s.stage_id }))} onChange={entry_stage => specPatch({ entry_stage })} /></label>
      <h3>角色槽位</h3>{Object.entries(fields.spec.slots).map(([key, slot]) => <div key={key}>{key} · {slot.role} · 人数
        <InputNumber aria-label={`${key} 最少人数`} disabled={locked} min={1} max={slot.role === 'expert' ? capability?.max_slot_size : 1} value={slot.minimum} onChange={v => specPatch({ slots: { ...fields.spec.slots, [key]: { ...slot, minimum: v ?? 1 } } })} /> 至
        <InputNumber aria-label={`${key} 最多人数`} disabled={locked} min={1} max={slot.role === 'expert' ? capability?.max_slot_size : 1} value={slot.maximum} onChange={v => specPatch({ slots: { ...fields.spec.slots, [key]: { ...slot, maximum: v ?? 1 } } })} />
      </div>)}
      {fields.spec.language_version === '2' && fields.spec.family === 'multi_expert' && <Checkbox aria-label="启用规划阶段" disabled={locked}
        checked={fields.spec.stages.some(s => s.kind === 'plan')} onChange={e => setFields({ ...fields,
          spec: togglePlanner(fields.spec, e.target.checked, templates.find(t => t.template_id === 'multi_expert')!.spec) })}>启用规划阶段</Checkbox>}
      {fields.spec.language_version === '2' && fields.spec.retry_limits && fields.spec.stages.some(s => s.routes.some(r => r.condition.endsWith('_retry'))) && <div>
        {(['retrieval', 'planning'] as const).map(key => <label key={key}>{key === 'retrieval' ? '补检索次数上限' : '重规划次数上限'}<InputNumber aria-label={`${key} 重试上限`} min={0} max={capability?.max_retry_count} precision={0} disabled={locked}
          value={fields.spec.retry_limits![key]} onChange={v => {
            const retry_limits = { ...fields.spec.retry_limits!, [key]: v ?? 0 }
            specPatch({ retry_limits, stages: fields.spec.stages.map(s => ({ ...s, max_visits: Math.max(s.max_visits,
              s.kind === 'plan' ? 1 + retry_limits.planning : ['retrieve', 'review'].includes(s.kind) ? 1 + retry_limits.retrieval + retry_limits.planning : s.max_visits) })) })
          }} /></label>)}
      </div>}
      <h3>执行阶段</h3><Collapse items={fields.spec.stages.map((s, index) => ({ key: index, label: `${s.stage_id} · ${s.kind}`, children: <div className="agent-fields">
        <label>阶段 ID<Input aria-label={`阶段 ${index + 1} ID`} value={s.stage_id} disabled={locked} onChange={e => stagePatch(index, { stage_id: e.target.value })} /></label>
        <label>阶段类型<Select disabled={locked} value={s.kind} options={(capability?.family_kinds[fields.spec.family] ?? capability?.stage_kinds ?? []).map(value => ({ value }))} onChange={kind => stagePatch(index, { kind,
          mode: capability?.parallel_kinds.includes(kind) ? s.mode : 'sequential' })} /></label>
        <label>参与槽位<Select disabled={locked} value={s.slot} options={Object.keys(fields.spec.slots).map(value => ({ value }))} onChange={slot => stagePatch(index, { slot })} /></label>
        <label>执行方式<Select aria-label={`${s.stage_id} 执行方式`} disabled={locked} value={s.mode} options={[{ value: 'sequential', label: '顺序' }, ...(capability?.parallel_kinds.includes(s.kind) ? [{ value: 'parallel', label: '并行后汇合' }] : [])]} onChange={mode => stagePatch(index, { mode })} /></label>
        <label>读取阶段<Select mode="multiple" disabled={locked} value={s.reads} options={fields.spec.stages.map(v => ({ value: v.stage_id }))} onChange={reads => stagePatch(index, { reads })} /></label>
        <label>阶段任务说明<Input.TextArea disabled={locked} value={s.instructions} maxLength={4000} onChange={e => stagePatch(index, { instructions: e.target.value })} /></label>
        <Checkbox disabled={locked} checked={s.share_evidence} onChange={e => stagePatch(index, { share_evidence: e.target.checked })}>共享所读阶段的原文证据</Checkbox>
        {s.kind === 'critique' && <label>质疑对象<Select aria-label={`${s.stage_id} 质疑对象`} disabled={locked} value={s.peer_policy ?? 'all'} options={[{ value: 'all', label: '所有其他专家' }, { value: 'next', label: '依次轮转的下一位专家' }]} onChange={peer_policy => stagePatch(index, { peer_policy })} /></label>}
        <label>最多执行次数<InputNumber disabled={locked} min={1} max={capability?.max_visits} precision={0} value={s.max_visits} onChange={v => stagePatch(index, { max_visits: v ?? 1 })} /></label>
        <label>超限出口<Select disabled={locked} value={s.on_limit} options={['END', ...fields.spec.stages.filter(v => capability?.limit_target_kinds.includes(v.kind)).map(v => v.stage_id)].map(value => ({ value }))} onChange={on_limit => stagePatch(index, { on_limit })} /></label>
        {s.routes.map((r, ri) => <Space key={ri} wrap><Select aria-label={`${s.stage_id} 条件 ${ri + 1}`} disabled={locked} value={r.condition} options={(capability?.conditions_by_kind[s.kind] ?? []).filter(c => fields.spec.family === 'pipeline' || !['insufficient_retry', 'conflict_retry'].includes(c)).map(value => ({ value }))} onChange={condition => stagePatch(index, { routes: s.routes.map((v, j) => j === ri ? { ...v, condition } : v) })} />
          <Select aria-label={`${s.stage_id} 目标 ${ri + 1}`} disabled={locked} value={r.target} options={['END', ...fields.spec.stages.map(v => v.stage_id)].map(value => ({ value }))} onChange={target => stagePatch(index, { routes: s.routes.map((v, j) => j === ri ? { ...v, target } : v) })} />
          <Button disabled={locked || s.routes.length === 1} onClick={() => stagePatch(index, { routes: s.routes.filter((_, j) => j !== ri) })}>删除出口</Button></Space>)}
        <Space wrap><Button disabled={locked || s.routes.length >= (capability?.max_routes ?? 0)} onClick={() => stagePatch(index, { routes: [...s.routes, { condition: 'fallback', target: 'END' }] })}>添加出口</Button>
          <Button disabled={locked || index === 0} onClick={() => { const stages = [...fields.spec.stages]; [stages[index - 1], stages[index]] = [stages[index], stages[index - 1]]; specPatch({ stages }) }}>上移列表位置</Button>
          <Button disabled={locked || fields.spec.stages.length <= (capability?.family_min_stages[fields.spec.family] ?? capability?.min_stages ?? 1)} onClick={() => specPatch({ stages: fields.spec.stages.filter((_, i) => i !== index) })}>删除阶段</Button></Space>
      </div> }))} />
      <Button disabled={locked || fields.spec.family === 'single_expert' || fields.spec.stages.length >= (capability?.max_stages ?? 0)} onClick={() => specPatch({ stages: [...fields.spec.stages, { ...structuredClone(fields.spec.stages[0]), stage_id: `stage_${Date.now()}` }] })}>添加阶段</Button>
      <p>执行顺序由入口和各阶段出口决定。调整列表位置只改变预览顺序。</p>
      <h3>执行保护</h3>{(Object.keys(fields.spec.limits) as (keyof ProtocolSpec['limits'])[]).map(key => <label key={key}>{({ max_node_calls: '节点调用上限', max_llm_calls: '模型调用上限', max_reserved_tokens: '保守 Token 预留预算', timeout_seconds: '运行超时（秒）', node_timeout_seconds: '单任务超时（秒）' })[key]}
        <InputNumber aria-label={key} disabled={locked} min={1} precision={0} value={fields.spec.limits[key]} onChange={v => specPatch({ limits: { ...fields.spec.limits, [key]: v ?? 1 } })} /></label>)}
      <ProtocolPreview spec={fields.spec} /><Space wrap>
        <Button disabled={locked} onClick={() => void act(async () => { await protocolRequest('/validate', 'POST', fields.spec); setNotice('协议校验通过。') })}>校验协议</Button>
        <Button type="primary" disabled={locked || !fields.name.trim()} onClick={() => void act(async () => {
          const saved = await protocolRequest<ManagedProtocol>(selected ? `/${selected.protocol_id}/draft` : '', selected ? 'PUT' : 'POST', { ...fields, ...(selected ? { expected_revision: selected.draft_revision } : {}) })
          setSelected(saved); setRelease(saved.versions.find(v => v.version === release?.version) ?? saved.versions[0]); setFields({ name: saved.draft.name, description: saved.draft.description, spec: saved.draft.spec }); await reload(); setNotice(`已保存 r${saved.draft_revision}。`)
        })}>保存协议草稿</Button>
        <Button disabled={locked || dirty || !selected} onClick={() => void act(async () => {
          const doc = await protocolRequest<ProtocolDocument>(`/${selected!.protocol_id}/versions`, 'POST', { expected_revision: selected!.draft_revision })
          setSelected(await protocolRequest(`/${selected!.protocol_id}`)); setRelease(doc); await reload(); setNotice(`已发布 ${doc.version}，可在我的系统绑定。`)
        })}>发布协议版本</Button>
        <Button disabled={disabled || busy} onClick={() => { setSelected(undefined); setRelease(undefined); setFields({ ...structuredClone(fields), name: `${fields.name.slice(0, 95)} 副本` }) }}>复制为新协议</Button>
        <Button disabled={locked || dirty || !selected} onClick={() => void act(async () => { setSelected(await protocolRequest(`/${selected!.protocol_id}/archive`, 'POST', { expected_revision: selected!.draft_revision })); await reload() })}>归档协议</Button>
      </Space>{selected && <><h3>已发布版本（只读）</h3>{selected.versions.length ? <Select className="protocol-release-select" aria-label="协议发布版本" placeholder="选择发布版本" value={release?.version} options={selected.versions.map(v => ({ value: v.version, label: v.version }))} onChange={v => setRelease(selected.versions.find(d => d.version === v))} /> : <p>暂无已发布版本</p>}
        {release && <><p>{release.name} · {release.version} · {release.spec_sha256}</p><ProtocolPreview spec={release.spec} /></>}</>}
    </Card>}</div></div>
  </main>
}
