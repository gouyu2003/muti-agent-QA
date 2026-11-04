import { useEffect, useState } from 'react'
import { Alert, Button, Collapse, Input, Space, Table } from 'antd'
import { evaluationRequest, type SavedEvaluationPlan } from './evaluationApi'

export default function EvaluationPlans({ request, disabled, jobId, onLoad }: {
  request: Record<string, unknown>; disabled: boolean; jobId?: string
  onLoad: (request: Record<string, unknown>, planId?: string) => Promise<void>
}) {
  const [items, setItems] = useState<SavedEvaluationPlan[]>([])
  const [name, setName] = useState('对比实验方案')
  const [file, setFile] = useState<File>()
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let disposed = false
    evaluationRequest<{ items: SavedEvaluationPlan[] }>('/plans').then(r => { if (!disposed) setItems(r.items ?? []) }).catch(e => { if (!disposed) setError(String(e)) })
    return () => { disposed = true }
  }, [])
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await fn() } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  function saved(plan: SavedEvaluationPlan) { setItems(old => [plan, ...old]); setNotice('已保存不可变方案；再次保存会产生新的方案记录。') }
  return <section aria-label="实验方案复用">
    <div className="evaluation-section-heading"><div><h2>可复用实验方案</h2><p>保存数据引用、对比对象和实验条件，载入后继续配置。</p></div></div>
    {error && <Alert type="error" title={error} />}{notice && <Alert type="success" title={notice} />}
    <Table<SavedEvaluationPlan> size="small" rowKey="id" dataSource={items} pagination={{ pageSize: 8 }} scroll={{ x: 620 }} columns={[
      { title: '方案名称', dataIndex: 'name' },
      { title: '对比对象', render: (_, plan) => Array.isArray(plan.request.targets) ? `${plan.request.targets.length} 个` : '—' },
      { title: '保存时间', dataIndex: 'created_at', render: value => new Date(value).toLocaleString() },
      { title: '操作', render: (_, plan) => <Space wrap>
        <Button type="link" size="small" disabled={busy} onClick={() => void action(async () => {
          await onLoad({ ...plan.request, name: `${String(plan.request.name ?? plan.name).slice(0, 96)} 副本` }, plan.id)
          setNotice('已载入新实验配置，请核对当前可用版本并重新校验、冻结。')
        })}>载入为新实验</Button>
        <a href={`/api/v1/evaluations/plans/${plan.id}/export`}>导出方案 JSON</a>
      </Space> },
    ]} />
    <Collapse items={[{ key: 'save', label: '保存或导入方案', children: <div className="evaluation-fields">
    <Space wrap>
      <Input aria-label="实验方案名称" value={name} maxLength={100} onChange={e => setName(e.target.value)} />
      <Button disabled={disabled || busy || !name.trim()} onClick={() => void action(async () => saved(await evaluationRequest<SavedEvaluationPlan>('/plans', 'POST', { name, request })))}>保存当前配置为方案</Button>
      <Button disabled={!jobId || busy || !name.trim()} onClick={() => void action(async () => saved(await evaluationRequest<SavedEvaluationPlan>('/plans', 'POST', { name, source_job_id: jobId })))}>保存所选任务方案</Button>
    </Space>
    <Space wrap>
      <label>导入平台方案 JSON<input aria-label="导入平台方案 JSON" type="file" accept=".json" onChange={e => setFile(e.target.files?.[0])} /></label>
      <Button disabled={!file || busy} onClick={() => void action(async () => {
        const source = JSON.parse(await file!.text())
        saved(await evaluationRequest<SavedEvaluationPlan>('/plans', 'POST', { name: source.name, request: source.request, sha256: source.sha256 }))
      })}>校验并保存导入方案</Button>
    </Space>
    </div> }]} />
    <small>载入只填充配置，不启动模型；题库、系统版本或环境变化后必须重新校验。跨环境复用需先准备对应数据与系统。</small>
  </section>
}
