import { useEffect, useState } from 'react'
import { Alert, Button, Collapse, Input, InputNumber, Segmented, Select, Space, Tabs } from 'antd'
import { evaluationRequest, type EvaluationManifest, type QuestionBank } from './evaluationApi'

const purposes = [{ value: 'smoke', label: '试跑' }, { value: 'development', label: '开发实验' }, { value: 'evaluation', label: '正式评价' }]
export default function EvaluationDataPlan({ bank, purpose, onPurpose, manifest, onManifest, showManagement = true }: {
  bank?: QuestionBank; purpose: EvaluationManifest['purpose']; onPurpose: (p: EvaluationManifest['purpose']) => void
  manifest?: EvaluationManifest; onManifest: (m?: EvaluationManifest) => void
  showManagement?: boolean
}) {
  const [items, setItems] = useState<EvaluationManifest[]>([])
  const [name, setName] = useState('固定题目清单')
  const [count, setCount] = useState<number | null>(null)
  const [seed, setSeed] = useState(42)
  const [sampling, setSampling] = useState('proportional')
  const [development, setDevelopment] = useState(100)
  const [evaluation, setEvaluation] = useState(1000)
  const [smoke, setSmoke] = useState(6)
  const [source, setSource] = useState<File>()
  const [exposureFile, setExposureFile] = useState<File>()
  const [exposureIds, setExposureIds] = useState('')
  const [reason, setReason] = useState('已用于开发或人工查看')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let disposed = false
    evaluationRequest<{ items: EvaluationManifest[] }>('/manifests').then(r => { if (!disposed) setItems(r.items ?? []) }).catch(e => { if (!disposed) setError(String(e)) })
    return () => { disposed = true }
  }, [bank?.bank_id, bank?.version])
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await fn() } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  const matching = items.filter(m => m.bank_id === bank?.bank_id && m.bank_version === bank?.version && m.purpose === purpose)
  const disabled = busy || !bank
  const binding = { bank_id: bank?.bank_id, bank_version: bank?.version }
  return <div className="evaluation-manifest-fields">
    <div className="evaluation-field"><span>实验用途</span><Segmented aria-label="实验用途" value={purpose} options={purposes}
      onChange={value => { onPurpose(value as EvaluationManifest['purpose']); onManifest(undefined) }} /></div>
    <label>固定题目清单<Select aria-label="固定题目清单" allowClear value={manifest?.manifest_id}
      placeholder={purpose === 'evaluation' ? '正式评价必须选择已冻结清单' : '可不选，直接使用题库或抽样'}
      options={matching.map(m => ({ value: m.manifest_id, label: `${m.name} · ${m.count} 题` }))}
      onChange={value => onManifest(matching.find(m => m.manifest_id === value))} /></label>
    {manifest && <small className="evaluation-fingerprint">{manifest.count} 题 · 题型 {JSON.stringify(manifest.type_counts)} · 清单指纹 {manifest.sha256}</small>}
    {purpose === 'evaluation' && !manifest && <p className="evaluation-hint">正式评价需要固定评价清单，已登记的开发、试跑和曝光题会从评价集中排除。</p>}
    {error && <Alert type="error" title={error} />}{notice && <Alert type="success" title={notice} />}
    {showManagement && <Collapse items={[{ key: 'data', label: '生成清单、划分数据与登记曝光', children: <>
      <Tabs items={[{ key: 'create', label: '生成清单', children: <div className="evaluation-fields">
      <label>清单名称<Input aria-label="清单名称" value={name} maxLength={80} onChange={e => setName(e.target.value)} /></label>
      <Space wrap>
        <label>清单题数（留空为全部符合条件的题）<InputNumber aria-label="清单题数" value={count} min={1} max={bank?.count ?? 10000} onChange={setCount} /></label>
        <label>抽样种子<InputNumber aria-label="抽样种子" value={seed} precision={0} onChange={v => setSeed(v ?? 42)} /></label>
        <label>抽样规则<Select aria-label="抽样规则" value={sampling} options={[{ value: 'proportional', label: '按题型比例' }, { value: 'random', label: '固定种子的全体抽样' }]} onChange={setSampling} /></label>
      </Space>
      <Button disabled={disabled || !name.trim()} onClick={() => void action(async () => {
        const result = await evaluationRequest<EvaluationManifest>('/manifests', 'POST', { ...binding, name, purpose, count, seed, sampling })
        setItems(old => [result, ...old]); onManifest(result); setNotice(`已固定 ${result.count} 道题，后续不会再次抽样。`)
      })}>生成并固定当前用途清单</Button>
      </div> }, { key: 'import', label: '导入清单', children: <div className="evaluation-fields">
      <label>清单名称<Input aria-label="导入清单名称" value={name} maxLength={80} onChange={e => setName(e.target.value)} /></label>
      <label className="evaluation-file-picker">导入已有清单 JSON<input aria-label="导入已有清单 JSON" type="file" accept=".json" onChange={e => setSource(e.target.files?.[0])} /></label>
      <small>支持平台导出的清单及原 CLI 清单；需与原始数据文件指纹匹配，并选择相同用途。</small>
      <Button disabled={disabled || !source || !name.trim()} onClick={() => void action(async () => {
        const result = await evaluationRequest<EvaluationManifest>('/manifests', 'POST', { ...binding, name, purpose, source_manifest: JSON.parse(await source!.text()) })
        setItems(old => [result, ...old]); onManifest(result); setNotice(`已按原清单固定 ${result.count} 道题。`)
      })}>校验并导入固定清单</Button>
      </div> }, { key: 'split', label: '数据划分', children: <div className="evaluation-fields">
      <label>划分名称<Input aria-label="划分名称" value={name} maxLength={80} onChange={e => setName(e.target.value)} /></label>
      <Space wrap>
        <label>抽样种子<InputNumber aria-label="划分抽样种子" value={seed} precision={0} onChange={value => setSeed(value ?? 42)} /></label>
        <label>抽样规则<Select aria-label="划分抽样规则" value={sampling} options={[{ value: 'proportional', label: '按题型比例' }, { value: 'random', label: '固定种子的全体抽样' }]} onChange={setSampling} /></label>
      </Space>
      <Space wrap>
        <label>开发题数<InputNumber aria-label="开发题数" min={1} value={development} onChange={v => setDevelopment(v ?? 1)} /></label>
        <label>评价题数<InputNumber aria-label="评价题数" min={1} value={evaluation} onChange={v => setEvaluation(v ?? 1)} /></label>
        <label>试跑题数（开发集子集）<InputNumber aria-label="试跑题数" min={1} value={smoke} onChange={v => setSmoke(v ?? 1)} /></label>
      </Space>
      <Button disabled={disabled || !name.trim()} onClick={() => void action(async () => {
        const result = await evaluationRequest<{ items: EvaluationManifest[] }>('/splits', 'POST', { ...binding, name, development, evaluation, smoke, seed, sampling })
        setItems(old => [...result.items, ...old]); onManifest(result.items.find(m => m.purpose === purpose)); setNotice('开发、评价及试跑清单已生成；评价与开发/已登记曝光题无交集。')
      })}>生成开发／评价／试跑划分</Button>
      </div> }, { key: 'exposure', label: '曝光登记', children: <div className="evaluation-fields">
      <label>已曝光题目 ID（逗号或换行分隔）<Input.TextArea aria-label="已曝光题目 ID" value={exposureIds} onChange={e => setExposureIds(e.target.value)} /></label>
      <label>或导入开发／试跑清单<input aria-label="曝光清单 JSON" type="file" accept=".json" onChange={e => setExposureFile(e.target.files?.[0])} /></label>
      <label>曝光登记原因<Input aria-label="曝光登记原因" value={reason} maxLength={1000} onChange={e => setReason(e.target.value)} /></label>
      <Button disabled={disabled || (!exposureIds.trim() && !exposureFile) || !reason.trim()} onClick={() => void action(async () => {
        const result = await evaluationRequest<{ count: number }>('/exposures', 'POST', { ...binding, reason,
          ...(exposureFile ? { source_manifest: JSON.parse(await exposureFile.text()) } : { sample_ids: exposureIds.split(/[,，\s]+/).filter(Boolean) }) })
        setNotice(`该题库已登记 ${result.count} 道开发或曝光题；同一题库各版本共用排除记录。`)
      })}>登记并从正式评价中排除</Button>
      </div> }]} />
    </> }]} />}
    {showManagement && <Space wrap>{matching.map(m => <a key={m.manifest_id} href={`/api/v1/evaluations/manifests/${m.manifest_id}/export`}>导出清单：{m.name}</a>)}</Space>}
  </div>
}
