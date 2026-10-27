import { useEffect, useState } from 'react'
import { Alert, Button, Checkbox, Collapse, Input, InputNumber, Select, Tag, Tooltip } from 'antd'
import { protocolRequest } from './api'
import { DeleteOutlined, SearchOutlined } from './Icons'
import { togglePlanner } from './protocolEditing'
import type { ManagedProtocol, ProtocolDocument, ProtocolCatalog, ProtocolSpec } from './protocolTypes'

export function ProtocolPreview({ spec }: { spec: ProtocolSpec }) {
  return <ol className="protocol-preview" aria-label="协议流程预览">{spec.stages.map((s, index) => <li key={s.stage_id}>
    <div className="protocol-preview-head">
      <span className="protocol-preview-index">{String(index + 1).padStart(2, '0')}</span>
      <strong>{s.stage_id}</strong>
      {s.stage_id === spec.entry_stage ? <Tag color="blue">入口</Tag> : null}
      <Tag>{s.kind}</Tag>
      <Tag>{s.mode === 'parallel' ? '并行后汇合' : '顺序'}</Tag>
    </div>
    <p className="protocol-preview-meta">{s.slot} · 最多 {s.max_visits} 次</p>
    <p className="protocol-preview-reads">读取：{s.reads.join('、') || '独立输入'}；{s.routes.map(r => `${r.condition} → ${r.target}`).join('；')}</p>
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
  const [query, setQuery] = useState('')
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
  const term = query.trim().toLowerCase()
  const visibleProtocols = term
    ? items.filter(p => p.draft.name.toLowerCase().includes(term) || p.draft.description.toLowerCase().includes(term))
    : items
  return <main className="systems-workspace">
    <header className="ws-page-head">
      <div className="ws-page-title">
        <span className="section-kicker">MY PROTOCOLS</span>
        <h1>我的协作协议</h1>
        <p>定义阶段、参与角色、共享范围和路由，再绑定到我的系统。所有执行规则由服务端校验。</p>
      </div>
      {onReturnToSystems ? <div className="ws-page-actions">
        <Button disabled={disabled || busy} onClick={onReturnToSystems}>返回我的系统</Button>
      </div> : null}
    </header>

    {(error || notice) ? <div className="ws-notices">
      {error ? <Alert type="error" title="协议操作未完成" description={error} /> : null}
      {notice ? <Alert type="success" title={notice} /> : null}
    </div> : null}

    <section className="ws-templates" aria-label="协议模板">
      <div className="ws-templates-head">
        <h2>从模板开始</h2>
        {templates.length ? <Tag>{templates.length} 个模板</Tag> : null}
      </div>
      <div className="protocol-template-grid">
        {templates.map((t, index) => <article className="ws-template-card" key={t.template_id}>
          <header>
            <span className="ws-template-index">{String(index + 1).padStart(2, '0')}</span>
            <strong>{t.name}</strong>
          </header>
          {t.description ? <p>{t.description}</p> : null}
          {Object.keys(t.spec.slots).length ? <p className="ws-template-meta">
            {Object.entries(t.spec.slots).map(([slot, c]) => `${slot}：${c.minimum}～${c.maximum}`).join(' · ')}
          </p> : null}
          <Button disabled={disabled || busy || !catalog.capabilities?.[t.spec.language_version]}
            onClick={() => adopt(undefined, t.spec)}>从 {t.name} 创建</Button>
        </article>)}
      </div>
    </section>

    <div className="ws-layout">
      <aside className="ws-rail" aria-label="协议列表">
        <div className="ws-rail-head">
          <h2>我的协议 <span className="ws-count">{items.length}</span></h2>
          <Input aria-label="搜索协议" placeholder="搜索协议名称" allowClear prefix={<SearchOutlined />}
            value={query} onChange={e => setQuery(e.target.value)} />
        </div>
        {visibleProtocols.length ? <div className="ws-rail-list">
          {visibleProtocols.map(p => <button key={p.protocol_id} type="button" disabled={disabled || busy}
            aria-current={selected?.protocol_id === p.protocol_id ? 'true' : undefined}
            className={`ws-rail-item${selected?.protocol_id === p.protocol_id ? ' is-active' : ''}`} onClick={() => adopt(p)}>
            <span className="ws-rail-copy">
              <strong>{p.draft.name}</strong>
              <small>r{p.draft_revision} · {p.versions[0]?.version ?? '未发布'}{p.archived ? ' · 已归档' : ''}</small>
            </span>
            <span className={`ws-rail-flag${!p.archived && p.versions.length ? ' is-live' : ''}`}>{p.archived ? '已归档' : p.versions.length ? '已发布' : '草稿'}</span>
          </button>)}
        </div> : <p className="ws-rail-empty">{term ? '没有匹配的协议。' : '尚未保存协议'}</p>}
      </aside>

      <div className="ws-canvas">
        {!fields ? <div className="ws-empty">
          <strong>尚未打开协议</strong>
          <p>选择左侧协议继续编辑，或从上方模板创建。</p>
        </div> : <>
          <div className="ws-sticky-shell">
          <section className="ws-toolbar" aria-label="协议编辑工具条">
            <div className="ws-toolbar-main">
              <span className="ws-overline">{selected ? (selected.archived ? '已归档协议' : '编辑协议草稿') : '新建协议'}</span>
              <h2>{fields.name.trim() || '未命名协议'}</h2>
              <div className="ws-badges">
                {dirty ? <Tag color="orange">有未保存修改</Tag> : selected ? <Tag>已保存</Tag> : null}
                {selected ? <Tag>r{selected.draft_revision}</Tag> : null}
                {selected?.archived ? <Tag>已归档</Tag> : null}
                {!capability ? <Tag color="red">编辑能力不可用</Tag> : null}
              </div>
            </div>
            <div className="ws-toolbar-actions">
              <Button disabled={disabled || busy} onClick={() => { setSelected(undefined); setRelease(undefined); setFields({ ...structuredClone(fields), name: `${fields.name.slice(0, 95)} 副本` }) }}>复制为新协议</Button>
              <Button danger disabled={locked || dirty || !selected} onClick={() => void act(async () => { setSelected(await protocolRequest(`/${selected!.protocol_id}/archive`, 'POST', { expected_revision: selected!.draft_revision })); await reload() })}>归档协议</Button>
            </div>
          </section>
          </div>

          <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">01</span><h3>基本信息</h3></header>
            <div className="ws-section-body">
              <div className="ws-field-grid">
                <label className="ws-field"><span>协议名称</span>
                  <Input aria-label="协议名称" disabled={locked} value={fields.name} maxLength={100} onChange={e => setFields({ ...fields, name: e.target.value })} /></label>
                <label className="ws-field"><span>入口阶段</span>
                  <Select aria-label="入口阶段" disabled={locked} value={fields.spec.entry_stage}
                    options={fields.spec.stages.map(s => ({ value: s.stage_id }))} onChange={entry_stage => specPatch({ entry_stage })} /></label>
                <label className="ws-field ws-field-wide"><span>协议说明</span>
                  <Input.TextArea aria-label="协议说明" disabled={locked} value={fields.description} maxLength={2000} onChange={e => setFields({ ...fields, description: e.target.value })} /></label>
              </div>
            </div>
          </section>

          <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">02</span><h3>角色槽位</h3>
              <div className="ws-section-extra"><Tag>{Object.keys(fields.spec.slots).length} 个槽位</Tag></div>
            </header>
            <div className="ws-section-body">
              <div className="ws-slot-grid">
                {Object.entries(fields.spec.slots).map(([key, slot]) => <div className="ws-slot" key={key}>
                  <div className="ws-slot-id"><strong>{key}</strong><span>{slot.role}</span></div>
                  <div className="ws-seat">
                    <span className="ws-seat-label">人数</span>
                    <InputNumber className="ws-num" aria-label={`${key} 最少人数`} disabled={locked} min={1} max={slot.role === 'expert' ? capability?.max_slot_size : 1}
                      value={slot.minimum} onChange={v => specPatch({ slots: { ...fields.spec.slots, [key]: { ...slot, minimum: v ?? 1 } } })} />
                    <span className="ws-seat-sep" aria-hidden="true">–</span>
                    <InputNumber className="ws-num" aria-label={`${key} 最多人数`} disabled={locked} min={1} max={slot.role === 'expert' ? capability?.max_slot_size : 1}
                      value={slot.maximum} onChange={v => specPatch({ slots: { ...fields.spec.slots, [key]: { ...slot, maximum: v ?? 1 } } })} />
                  </div>
                </div>)}
              </div>
            </div>
          </section>

          <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">03</span><h3>执行阶段</h3>
              <div className="ws-section-extra">
                {fields.spec.language_version === '2' && fields.spec.family === 'multi_expert' && <Checkbox aria-label="启用规划阶段" disabled={locked}
                  checked={fields.spec.stages.some(s => s.kind === 'plan')} onChange={e => setFields({ ...fields,
                    spec: togglePlanner(fields.spec, e.target.checked, templates.find(t => t.template_id === 'multi_expert')!.spec) })}>启用规划阶段</Checkbox>}
                <Tag>{fields.spec.stages.length} 个阶段</Tag>
                <Button disabled={locked || fields.spec.family === 'single_expert' || fields.spec.stages.length >= (capability?.max_stages ?? 0)}
                  onClick={() => specPatch({ stages: [...fields.spec.stages, { ...structuredClone(fields.spec.stages[0]), stage_id: `stage_${Date.now()}` }] })}>添加阶段</Button>
              </div>
            </header>
            <div className="ws-section-body">
              {fields.spec.language_version === '2' && fields.spec.retry_limits && fields.spec.stages.some(s => s.routes.some(r => r.condition.endsWith('_retry'))) && <div className="ws-inline-fields">
                {(['retrieval', 'planning'] as const).map(key => <label className="ws-field" key={key}>
                  <span>{key === 'retrieval' ? '补检索次数上限' : '重规划次数上限'}</span>
                  <InputNumber aria-label={`${key} 重试上限`} min={0} max={capability?.max_retry_count} precision={0} disabled={locked}
                    value={fields.spec.retry_limits![key]} onChange={v => {
                      const retry_limits = { ...fields.spec.retry_limits!, [key]: v ?? 0 }
                      specPatch({ retry_limits, stages: fields.spec.stages.map(s => ({ ...s, max_visits: Math.max(s.max_visits,
                        s.kind === 'plan' ? 1 + retry_limits.planning : ['retrieve', 'review'].includes(s.kind) ? 1 + retry_limits.retrieval + retry_limits.planning : s.max_visits) })) })
                    }} />
                </label>)}
              </div>}
              <Collapse className="ws-stage-collapse" defaultActiveKey={[0]} items={fields.spec.stages.map((s, index) => ({ key: index,
                label: <span className="ws-stage-label">
                  <span className="ws-stage-index">{String(index + 1).padStart(2, '0')}</span>
                  <strong>{s.stage_id}</strong>
                  <Tag>{s.kind}</Tag>
                  <Tag>{s.mode === 'parallel' ? '并行' : '顺序'}</Tag>
                  <Tag>{s.slot}</Tag>
                </span>,
                children: <div className="ws-stage-body">
                  <div className="ws-form-grid">
                    <label className="ws-field"><span>阶段 ID</span>
                      <Input aria-label={`阶段 ${index + 1} ID`} value={s.stage_id} disabled={locked} onChange={e => stagePatch(index, { stage_id: e.target.value })} /></label>
                    <label className="ws-field"><span>阶段类型</span>
                      <Select disabled={locked} value={s.kind} options={(capability?.family_kinds[fields.spec.family] ?? capability?.stage_kinds ?? []).map(value => ({ value }))} onChange={kind => stagePatch(index, { kind,
                        mode: capability?.parallel_kinds.includes(kind) ? s.mode : 'sequential' })} /></label>
                    <label className="ws-field"><span>参与槽位</span>
                      <Select disabled={locked} value={s.slot} options={Object.keys(fields.spec.slots).map(value => ({ value }))} onChange={slot => stagePatch(index, { slot })} /></label>
                    <label className="ws-field"><span>执行方式</span>
                      <Select aria-label={`${s.stage_id} 执行方式`} disabled={locked} value={s.mode}
                        options={[{ value: 'sequential', label: '顺序' }, ...(capability?.parallel_kinds.includes(s.kind) ? [{ value: 'parallel', label: '并行后汇合' }] : [])]}
                        onChange={mode => stagePatch(index, { mode })} /></label>
                    <label className="ws-field ws-field-wide"><span>读取阶段</span>
                      <Select mode="multiple" disabled={locked} value={s.reads} options={fields.spec.stages.map(v => ({ value: v.stage_id }))} onChange={reads => stagePatch(index, { reads })} /></label>
                    <label className="ws-field ws-field-wide"><span>阶段任务说明</span>
                      <Input.TextArea disabled={locked} value={s.instructions} maxLength={4000} onChange={e => stagePatch(index, { instructions: e.target.value })} /></label>
                    <label className="ws-field"><span>最多执行次数</span>
                      <InputNumber disabled={locked} min={1} max={capability?.max_visits} precision={0} value={s.max_visits} onChange={v => stagePatch(index, { max_visits: v ?? 1 })} /></label>
                    <label className="ws-field"><span>超限出口</span>
                      <Select disabled={locked} value={s.on_limit} options={['END', ...fields.spec.stages.filter(v => capability?.limit_target_kinds.includes(v.kind)).map(v => v.stage_id)].map(value => ({ value }))} onChange={on_limit => stagePatch(index, { on_limit })} /></label>
                    {s.kind === 'critique' && <label className="ws-field"><span>质疑对象</span>
                      <Select aria-label={`${s.stage_id} 质疑对象`} disabled={locked} value={s.peer_policy ?? 'all'}
                        options={[{ value: 'all', label: '所有其他专家' }, { value: 'next', label: '依次轮转的下一位专家' }]} onChange={peer_policy => stagePatch(index, { peer_policy })} /></label>}
                  </div>
                  <Checkbox disabled={locked} checked={s.share_evidence} onChange={e => stagePatch(index, { share_evidence: e.target.checked })}>共享所读阶段的原文证据</Checkbox>
                  <div className="ws-route-block">
                    <div className="ws-route-head">
                      <span>出口路由</span>
                      <Button size="small" disabled={locked || s.routes.length >= (capability?.max_routes ?? 0)}
                        onClick={() => stagePatch(index, { routes: [...s.routes, { condition: 'fallback', target: 'END' }] })}>添加出口</Button>
                    </div>
                    {s.routes.map((r, ri) => <div className="ws-route-row" key={ri}>
                      <span className="ws-route-index">{ri + 1}</span>
                      <Select aria-label={`${s.stage_id} 条件 ${ri + 1}`} disabled={locked} value={r.condition}
                        options={(capability?.conditions_by_kind[s.kind] ?? []).filter(c => fields.spec.family === 'pipeline' || !['insufficient_retry', 'conflict_retry'].includes(c)).map(value => ({ value }))}
                        onChange={condition => stagePatch(index, { routes: s.routes.map((v, j) => j === ri ? { ...v, condition } : v) })} />
                      <span className="ws-route-arrow" aria-hidden="true">→</span>
                      <Select aria-label={`${s.stage_id} 目标 ${ri + 1}`} disabled={locked} value={r.target}
                        options={['END', ...fields.spec.stages.map(v => v.stage_id)].map(value => ({ value }))}
                        onChange={target => stagePatch(index, { routes: s.routes.map((v, j) => j === ri ? { ...v, target } : v) })} />
                      <Tooltip title="删除出口"><Button type="text" danger aria-label="删除出口" disabled={locked || s.routes.length === 1}
                        icon={<DeleteOutlined />} onClick={() => stagePatch(index, { routes: s.routes.filter((_, j) => j !== ri) })} /></Tooltip>
                    </div>)}
                  </div>
                  <div className="ws-row-actions">
                    <Button disabled={locked || index === 0} onClick={() => { const stages = [...fields.spec.stages]; [stages[index - 1], stages[index]] = [stages[index], stages[index - 1]]; specPatch({ stages }) }}>上移列表位置</Button>
                    <Button disabled={locked || fields.spec.stages.length <= (capability?.family_min_stages[fields.spec.family] ?? capability?.min_stages ?? 1)}
                      onClick={() => specPatch({ stages: fields.spec.stages.filter((_, i) => i !== index) })}>删除阶段</Button>
                  </div>
                </div> }))} />
              <p className="ws-hint">执行顺序由入口和各阶段出口决定。调整列表位置只改变预览顺序。</p>
            </div>
          </section>

          <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">04</span><h3>执行保护</h3></header>
            <div className="ws-section-body">
              <div className="ws-form-grid ws-form-grid-3">
                {(Object.keys(fields.spec.limits) as (keyof ProtocolSpec['limits'])[]).map(key => <label className="ws-field" key={key}>
                  <span>{({ max_node_calls: '节点调用上限', max_llm_calls: '模型调用上限', max_reserved_tokens: '保守 Token 预留预算', timeout_seconds: '运行超时（秒）', node_timeout_seconds: '单任务超时（秒）' })[key]}</span>
                  <InputNumber aria-label={key} disabled={locked} min={1} precision={0} value={fields.spec.limits[key]}
                    onChange={v => specPatch({ limits: { ...fields.spec.limits, [key]: v ?? 1 } })} />
                </label>)}
              </div>
            </div>
          </section>

          <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">05</span><h3>流程预览</h3></header>
            <div className="ws-section-body"><ProtocolPreview spec={fields.spec} /></div>
          </section>

          {selected ? <section className="ws-section">
            <header className="ws-section-head"><span className="ws-step">06</span><h3>已发布版本（只读）</h3>
              {selected.archived ? <div className="ws-section-extra"><Tag>已归档</Tag></div> : null}
            </header>
            <div className="ws-section-body">
              {selected.versions.length ? <label className="ws-field ws-field-inline"><span>发布版本</span>
                <Select className="protocol-release-select" aria-label="协议发布版本" placeholder="选择发布版本" value={release?.version}
                  options={selected.versions.map(v => ({ value: v.version, label: v.version }))} onChange={v => setRelease(selected.versions.find(d => d.version === v))} />
              </label> : <p>暂无已发布版本</p>}
              {release ? <>
                <p className="ws-hint">{release.name} · {release.version} · {release.spec_sha256}</p>
                <ProtocolPreview spec={release.spec} />
              </> : null}
            </div>
          </section> : null}

          <footer className="ws-action-bar">
            <p className="ws-action-note">{dirty ? '修改尚未保存；已发布版本不可覆盖。' : selected?.archived ? '协议已归档，只能查看或复制。' : '保存草稿后才能发布版本。'}</p>
            <div className="ws-action-buttons">
              <Button disabled={locked} onClick={() => void act(async () => { await protocolRequest('/validate', 'POST', fields.spec); setNotice('协议校验通过。') })}>校验协议</Button>
              <Button disabled={locked || dirty || !selected} onClick={() => void act(async () => {
                const doc = await protocolRequest<ProtocolDocument>(`/${selected!.protocol_id}/versions`, 'POST', { expected_revision: selected!.draft_revision })
                setSelected(await protocolRequest(`/${selected!.protocol_id}`)); setRelease(doc); await reload(); setNotice(`已发布 ${doc.version}，可在我的系统绑定。`)
              })}>发布协议版本</Button>
              <Button type="primary" disabled={locked || !fields.name.trim()} onClick={() => void act(async () => {
                const saved = await protocolRequest<ManagedProtocol>(selected ? `/${selected.protocol_id}/draft` : '', selected ? 'PUT' : 'POST', { ...fields, ...(selected ? { expected_revision: selected.draft_revision } : {}) })
                setSelected(saved); setRelease(saved.versions.find(v => v.version === release?.version) ?? saved.versions[0]); setFields({ name: saved.draft.name, description: saved.draft.description, spec: saved.draft.spec }); await reload(); setNotice(`已保存 r${saved.draft_revision}。`)
              })}>保存协议草稿</Button>
            </div>
          </footer>
        </>}
      </div>
    </div>
  </main>
}
