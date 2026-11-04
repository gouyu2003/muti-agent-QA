import { useEffect, useState } from 'react'
import { Alert, Button, Input, Select, Space } from 'antd'
import { evaluationRequest, type SavedEvaluationPlan } from './evaluationApi'

export default function EvaluationPlans({ request, disabled, jobId, onLoad }: {
  request: Record<string, unknown>; disabled: boolean; jobId?: string
  onLoad: (request: Record<string, unknown>, planId?: string) => Promise<void>
}) {
  const [items, setItems] = useState<SavedEvaluationPlan[]>([])
  const [selected, setSelected] = useState<string>()
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
  function saved(plan: SavedEvaluationPlan) { setItems(old => [plan, ...old]); setSelected(plan.id); setNotice('已保存不可变方案；再次保存会产生新的方案记录。') }
  return <section aria-label="实验方案复用">
    <h3>实验方案</h3>
    {error && <Alert type="error" title={error} />}{notice && <Alert type="success" title={notice} />}
    <Space wrap>
      <Input aria-label="实验方案名称" value={name} maxLength={100} onChange={e => setName(e.target.value)} />
      <Button disabled={disabled || busy || !name.trim()} onClick={() => void action(async () => saved(await evaluationRequest<SavedEvaluationPlan>('/plans', 'POST', { name, request })))}>保存当前配置为方案</Button>
      <Button disabled={!jobId || busy || !name.trim()} onClick={() => void action(async () => saved(await evaluationRequest<SavedEvaluationPlan>('/plans', 'POST', { name, source_job_id: jobId })))}>保存所选任务方案</Button>
    </Space>
    <Space wrap>
      <Select aria-label="已保存实验方案" allowClear placeholder="选择已保存方案" value={selected} onChange={setSelected}
        options={items.map(p => ({ value: p.id, label: `${p.name} · ${new Date(p.created_at).toLocaleString()}` }))} />
      <Button disabled={!selected || busy} onClick={() => void action(async () => {
        const plan = items.find(p => p.id === selected)!
        await onLoad({ ...plan.request, name: `${String(plan.request.name ?? plan.name).slice(0, 96)} 副本` }, plan.id)
        setNotice('已载入新实验配置；请核对当前可用版本，重新校验并冻结。原任务不会改变。')
      })}>载入为新实验</Button>
      {selected && <a href={`/api/v1/evaluations/plans/${selected}/export`}>导出方案 JSON</a>}
    </Space>
    <Space wrap>
      <label>导入平台方案 JSON<input aria-label="导入平台方案 JSON" type="file" accept=".json" onChange={e => setFile(e.target.files?.[0])} /></label>
      <Button disabled={!file || busy} onClick={() => void action(async () => {
        const source = JSON.parse(await file!.text())
        saved(await evaluationRequest<SavedEvaluationPlan>('/plans', 'POST', { name: source.name, request: source.request, sha256: source.sha256 }))
      })}>校验并保存导入方案</Button>
    </Space>
    <small>载入只填充配置，不启动模型；题库、系统版本或环境变化后必须重新校验。跨环境复用需先准备对应数据与系统。</small>
  </section>
}
