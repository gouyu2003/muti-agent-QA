import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Checkbox, Collapse, ConfigProvider, Empty, Input, InputNumber, Modal, Segmented, Select, Skeleton, Space, Steps } from 'antd'
import { evaluationRequest, type EvaluationJob, type EvaluationOptions, type EvaluationResult, type QuestionBank, type EvaluationManifest, type EvaluationPreflight } from './evaluationApi'
import EvaluationDataPlan from './EvaluationDataPlan'
import EvaluationPlans from './EvaluationPlans'
import EvaluationArchives from './EvaluationArchives'
import { METHOD_NAMES } from './evaluationLabels'
import EvaluationRunDetail from './EvaluationRunDetail'
import type { StoredRunDetail } from './types'
import { Plus, ChevronRight } from 'lucide-react'
import { ReloadOutlined, SearchOutlined } from './Icons'
import EvaluationLibrary from './EvaluationLibrary'
import EvaluationJobWorkspace, { EVALUATION_PURPOSE, EVALUATION_STATUS as STATUS } from './EvaluationJobWorkspace'
import './evaluations.css'

const message = (error: unknown) => error instanceof Error ? error.message : '操作失败'

type Workspace = 'tasks' | 'data' | 'plans' | 'archives' | 'create'

export default function EvaluationsView() {
  const [view, setView] = useState<Workspace>('tasks')
  const [step, setStep] = useState(0)
  const [savingPlan, setSavingPlan] = useState(false)
  const [planName, setPlanName] = useState('对比实验方案')
  const detailBase = useRef<string | undefined>(undefined)
  const [banks, setBanks] = useState<QuestionBank[]>([])
  const [options, setOptions] = useState<EvaluationOptions>({ methods: [], systems: [] })
  const [jobs, setJobs] = useState<EvaluationJob[]>([])
  const [job, setJob] = useState<EvaluationJob>()
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [bankKey, setBankKey] = useState<string>()
  const [name, setName] = useState('协议批量评测')
  const [kind, setKind] = useState<'builtin' | 'system' | 'mixed'>('mixed')
  const [selected, setSelected] = useState<string[]>([])
  const [contextMode, setContextMode] = useState('question_context')
  const [override, setOverride] = useState(true)
  const [model, setModel] = useState('deepseek-v4.1-flash')
  const [temperature, setTemperature] = useState(0)
  const [maxTokens, setMaxTokens] = useState(4096)
  const [topK, setTopK] = useState(3)
  const [repeats, setRepeats] = useState(1)
  const [sampleSize, setSampleSize] = useState<number | null>(null)
  const [purpose, setPurpose] = useState<EvaluationManifest['purpose']>('development')
  const [manifest, setManifest] = useState<EvaluationManifest>()
  const [preview, setPreview] = useState<QuestionBank['preview']>([])
  const [preflight, setPreflight] = useState<{ key: string; data: EvaluationPreflight }>()
  const [seed, setSeed] = useState(42)
  const [outputRetries, setOutputRetries] = useState(2)
  const [primaryMetric, setPrimaryMetric] = useState('f1')
  const [bootstrapSamples, setBootstrapSamples] = useState(2000)
  const [referenceIndex, setReferenceIndex] = useState<number | null>(null)
  const [executionMode, setExecutionMode] = useState('sequential')
  const [advanced, setAdvanced] = useState({ expert_count: 2, retrieval_retries: 3, plan_retries: 2, lead_rounds: 2, debate_rounds: 2 })
  const [limits, setLimits] = useState({ max_node_calls: 60, max_llm_calls: 40, max_reserved_tokens: 1000000, timeout_seconds: 600, node_timeout_seconds: 120 })
  const [showArchived, setShowArchived] = useState(false)
  const [jobSearch, setJobSearch] = useState('')
  const [jobStatus, setJobStatus] = useState<string>()
  const [jobPurpose, setJobPurpose] = useState<string>()
  const [savedPlanId, setSavedPlanId] = useState<string>()
  const [copiedFromJobId, setCopiedFromJobId] = useState<string>()
  const [detail, setDetail] = useState<StoredRunDetail>()
  const [detailTarget, setDetailTarget] = useState<EvaluationResult>()
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const detailRequest = useRef(0)
  const refreshRequest = useRef(0)
  const jobRequest = useRef(0)
  const actionInFlight = useRef(false)
  const selectedBank = banks.find(b => `${b.bank_id}:${b.version}` === bankKey)
  const planned = selected.length * (manifest?.count ?? sampleSize ?? selectedBank?.count ?? 0) * repeats
  const isBuiltin = (value: string) => options.methods.includes(value)
  const hasBuiltin = selected.some(isBuiltin)
  const invalidSystemSelection = selected.filter(value => !isBuiltin(value)).some(value => !options.systems.some(
    s => `${s.system_id}:${s.version}` === value && s.available !== false))
  const payload = {
    name, bank_id: selectedBank?.bank_id, bank_version: selectedBank?.version, purpose, manifest_id: manifest?.manifest_id ?? null,
    targets: selected.map(value => isBuiltin(value) ? { kind: 'builtin', method: value } : {
      kind: 'system', system_id: options.systems.find(s => `${s.system_id}:${s.version}` === value)?.system_id,
      version: options.systems.find(s => `${s.system_id}:${s.version}` === value)?.version,
    }), context_mode: contextMode, sample_size: manifest ? null : sampleSize, repeats, seed, top_k: topK,
    model: override ? { name: model, temperature, max_tokens: maxTokens, output_retries: outputRetries } : null,
    ...advanced, limits, execution_mode: executionMode, primary_metric: primaryMetric,
    bootstrap_samples: bootstrapSamples, reference_index: referenceIndex, saved_plan_id: savedPlanId ?? null,
    copied_from_job_id: copiedFromJobId ?? null,
  }
  const payloadKey = JSON.stringify(payload)
  const checkedPlan = preflight?.key === payloadKey ? preflight.data : undefined
  const invalidPlan = busy || !selectedBank || !selected.length || invalidSystemSelection || !name.trim() ||
    (override && !model.trim()) || (purpose === 'evaluation' && !manifest) || planned > 30000
  function selectBank(value: string) { setBankKey(value); setManifest(undefined); setPreview([]); setSampleSize(null) }
  const dataReady = !!selectedBank && !!name.trim() && (purpose !== 'evaluation' || !!manifest)
  const targetsReady = selected.length > 0 && !invalidSystemSelection
  const nextDisabled = busy || (step === 0 && !dataReady) || (step === 1 && !targetsReady) || (step === 2 && invalidPlan)
  function navigate(next: Workspace) {
    closeDetail()
    if (next === 'data' && !selectedBank && banks[0]) selectBank(`${banks[0].bank_id}:${banks[0].version}`)
    setView(next)
  }
  function newExperiment() {
    if (!selectedBank && banks[0]) selectBank(`${banks[0].bank_id}:${banks[0].version}`)
    setStep(0); setError(''); setNotice(''); setPreflight(undefined); navigate('create')
  }
  function selectTarget(value: string, checked: boolean) {
    const next = checked ? [...selected, value] : selected.filter(item => item !== value)
    setSelected(next); setReferenceIndex(null)
    if (next.some(isBuiltin)) { setContextMode('question_context'); setOverride(true) }
    else if (hasBuiltin || selected.length === 0) { setContextMode('system_sources'); setOverride(false) }
  }

  async function refresh() {
    const request = ++refreshRequest.current
    const [b, o, j] = await Promise.all([
      evaluationRequest<{ items: QuestionBank[] }>('/question-banks'),
      evaluationRequest<EvaluationOptions>('/options'),
      evaluationRequest<{ items: EvaluationJob[] }>(showArchived ? '/jobs?include_archived=true' : '/jobs'),
    ])
    if (request !== refreshRequest.current) return
    setBanks(b.items); setOptions(o); setJobs(j.items ?? [])
    setJob(current => current ?? j.items?.[0])
  }
  async function action(fn: () => Promise<void>) {
    actionInFlight.current = true; jobRequest.current += 1
    setBusy(true); setError('')
    try { await fn(); return true } catch (e) { setError(message(e)); return false }
    finally { actionInFlight.current = false; jobRequest.current += 1; setBusy(false) }
  }
  useEffect(() => { refresh().catch(e => setError(message(e))) }, [showArchived])
  useEffect(() => () => { detailRequest.current += 1; refreshRequest.current += 1 }, [])
  useEffect(() => {
    if (!job || view !== 'tasks') return
    let disposed = false
    async function load() {
      if (actionInFlight.current) return
      const request = ++jobRequest.current
      try {
        const current = await evaluationRequest<EvaluationJob>(`/jobs/${job!.job_id}`)
        if (!disposed && request === jobRequest.current) {
          setJob(current)
          setJobs(previous => previous.map(j => j.job_id === current.job_id ? current : j))
        }
      } catch (e) { if (!disposed && request === jobRequest.current) setError(message(e)) }
    }
    void load()
    const timer = window.setInterval(() => void load(), 3000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [job?.job_id, view])

  function adopt(item: EvaluationJob) {
    closeDetail(); setJob(item); setNotice(''); setView('tasks')
  }

  function closeDetail() {
    detailRequest.current += 1
    setDetailTarget(undefined); setDetail(undefined); setDetailError(''); setDetailLoading(false)
  }

  async function openDetail(row: EvaluationResult, sourceBase?: string) {
    const base = sourceBase ?? (job ? `/jobs/${job.job_id}` : undefined)
    if (!base) return
    detailBase.current = base
    const request = ++detailRequest.current
    setDetailTarget(row); setDetail(undefined); setDetailError(''); setDetailLoading(true)
    try {
      const result = await evaluationRequest<StoredRunDetail>(`${base}/runs/${row.run_id}`)
      if (request === detailRequest.current) setDetail(result)
    } catch (error) {
      if (request === detailRequest.current) setDetailError(message(error))
    } finally {
      if (request === detailRequest.current) setDetailLoading(false)
    }
  }

  async function loadPlan(request: Record<string, unknown>, planId?: string) {
    const targets = request.targets as { kind: string; method?: string; system_id?: string; version?: string }[]
    const manifests = request.manifest_id ? await evaluationRequest<{ items: EvaluationManifest[] }>('/manifests') : { items: [] }
    const selectedManifest = manifests.items.find(m => m.manifest_id === request.manifest_id)
    const modelConfig = request.model as { name: string; temperature: number; max_tokens: number; output_retries: number } | null
    const number = (key: string, fallback: number) => typeof request[key] === 'number' ? request[key] as number : fallback
    setName(String(request.name)); setBankKey(`${request.bank_id}:${request.bank_version}`); setManifest(selectedManifest); setPreview([])
    const builtin = targets.some(t => t.kind === 'builtin'); const system = targets.some(t => t.kind === 'system')
    setKind(builtin && system ? 'mixed' : builtin ? 'builtin' : 'system')
    setSelected(targets.map(t => t.kind === 'builtin' ? t.method! : `${t.system_id}:${t.version}`))
    setPurpose((request.purpose as EvaluationManifest['purpose']) ?? 'development')
    setContextMode(String(request.context_mode ?? 'question_context'))
    setOverride(!!modelConfig); setModel(modelConfig?.name ?? 'deepseek-v4.1-flash'); setTemperature(modelConfig?.temperature ?? 0)
    setMaxTokens(modelConfig?.max_tokens ?? 4096); setOutputRetries(modelConfig?.output_retries ?? 2)
    setSampleSize(typeof request.sample_size === 'number' ? request.sample_size : null); setRepeats(number('repeats', 1)); setSeed(number('seed', 42)); setTopK(number('top_k', 3))
    setPrimaryMetric(String(request.primary_metric ?? 'f1')); setBootstrapSamples(number('bootstrap_samples', 2000))
    setReferenceIndex(typeof request.reference_index === 'number' ? request.reference_index : null)
    setExecutionMode(String(request.execution_mode ?? 'sequential'))
    setAdvanced({ expert_count: number('expert_count', 2), retrieval_retries: number('retrieval_retries', 3), plan_retries: number('plan_retries', 2), lead_rounds: number('lead_rounds', 2), debate_rounds: number('debate_rounds', 2) })
    setLimits((request.limits as typeof limits) ?? { max_node_calls: 60, max_llm_calls: 40, max_reserved_tokens: 1000000, timeout_seconds: 600, node_timeout_seconds: 120 })
    setSavedPlanId(planId); setCopiedFromJobId(request.copied_from_job_id as string | undefined)
    setPreflight(undefined); closeDetail(); setStep(0); setView('create')
    setError(!banks.some(b => b.bank_id === request.bank_id && b.version === request.bank_version) || (request.manifest_id && !selectedManifest)
      ? '源方案绑定的题库或清单在当前环境不可用，请重新选择并校验。' : '')
    setNotice('已载入新实验配置。请核对当前数据与版本并重新校验、冻结；原任务与原成绩保持不变。')
  }
  const visibleJobs = jobs.filter(j => (!jobSearch || `${j.name} ${j.job_id}`.toLowerCase().includes(jobSearch.toLowerCase())) &&
    (!jobStatus || j.status === jobStatus) && (!jobPurpose || (j.plan?.request.purpose ?? 'legacy') === jobPurpose))

  const selectedLabels = selected.map(value => {
    const system = options.systems.find(item => `${item.system_id}:${item.version}` === value)
    return METHOD_NAMES[value] ?? (system ? `${system.name} · ${system.version}` : value)
  })

  return <ConfigProvider theme={{ cssVar: { key: 'evaluation-console' }, token: { controlHeight: 36, borderRadius: 8, fontSize: 14 }, components: { Button: { controlHeight: 36 }, Card: { bodyPadding: 22 } } }}>
    <main className="evaluations-page evaluation-console">
      <header className="evaluations-heading"><div><h1>批量评测</h1><p>从固定实验方案，到可追溯的对比结果</p></div><div className="evaluation-button-row">
        <Button type="text" icon={<ReloadOutlined />} disabled={busy} onClick={() => void action(refresh)}>刷新列表</Button>
        {view !== 'create' && <Button type="primary" icon={<Plus size={16} aria-hidden="true" />} onClick={newExperiment}>新建实验</Button>}
      </div></header>
      {error && <Alert type="error" showIcon title={error} closable onClose={() => setError('')} />}
      {notice && <Alert type="info" showIcon title={notice} closable onClose={() => setNotice('')} />}
      <nav className="evaluation-workspace-tabs" aria-label="批量评测功能">
        {[
          { key: 'tasks', label: <span>实验任务 <span className="evaluation-tab-count">{jobs.length}</span></span> },
          { key: 'data', label: '题库与清单' }, { key: 'plans', label: '实验方案' }, { key: 'archives', label: '历史档案' },
        ].map(item => <Button type="text" key={item.key} aria-current={item.key === (view === 'create' ? 'tasks' : view) ? 'page' : undefined} onClick={() => navigate(item.key as Workspace)}>{item.label}</Button>)}
      </nav>
      {view === 'tasks' && <div className="evaluation-task-layout">
        <aside className="evaluation-task-rail" aria-label="评测任务">
          <Input aria-label="搜索实验任务" prefix={<SearchOutlined />} placeholder="搜索名称或 ID" value={jobSearch} onChange={event => setJobSearch(event.target.value)} allowClear />
          <Select aria-label="任务状态筛选" allowClear placeholder="全部状态" value={jobStatus} onChange={setJobStatus} options={Object.entries(STATUS).map(([value, label]) => ({ value, label }))} />
          <Collapse ghost size="small" items={[{ key: 'filters', label: '更多筛选', children: <div className="evaluation-fields">
            <Select aria-label="任务用途筛选" allowClear placeholder="全部用途" value={jobPurpose} onChange={setJobPurpose} options={[
              ...Object.entries(EVALUATION_PURPOSE).map(([value, label]) => ({ value, label })), { value: 'legacy', label: '历史未标用途' },
            ]} /><Checkbox checked={showArchived} onChange={event => setShowArchived(event.target.checked)}>显示已归档任务</Checkbox>
          </div> }]} />
          {!visibleJobs.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的任务" /> : <div className="evaluation-job-list">{visibleJobs.map(item =>
            <button type="button" key={item.job_id} disabled={busy} aria-pressed={item.job_id === job?.job_id} className={item.job_id === job?.job_id ? 'selected' : ''} onClick={() => adopt(item)}>
              <strong>{item.name}</strong><span className="evaluation-list-status">{item.archived ? '已归档 · ' : ''}{STATUS[item.status] ?? item.status}<span>{item.progress.attempted} / {item.progress.planned}</span></span>
              <span>{EVALUATION_PURPOSE[String(item.plan?.request.purpose)] ?? '历史未标用途'} · {item.targets.length} 个对比对象</span>
            </button>)}</div>}
        </aside>
        {job ? <EvaluationJobWorkspace key={job.job_id} job={job} busy={busy} perform={action} onUpdate={updated => {
          setJob(updated); setJobs(previous => previous.map(item => item.job_id === updated.job_id ? updated : item))
        }} onCopy={() => void action(async () => {
          const source = await evaluationRequest<{ request: Record<string, unknown> }>(`/jobs/${job.job_id}/copy-plan`, 'POST')
          await loadPlan(source.request)
        })} onArchive={() => void action(async () => {
          setJob(await evaluationRequest<EvaluationJob>(`/jobs/${job.job_id}/archive`, 'POST', { archived: !job.archived })); await refresh()
        })} onOpenDetail={row => void openDetail(row)} /> : <section className="evaluation-panel evaluation-empty-workspace"><Empty description="新建实验或选择已有任务，查看对比结果" /></section>}
      </div>}
      {view === 'data' && <EvaluationLibrary banks={banks} selected={selectedBank} onSelect={selectBank} purpose={purpose} onPurpose={setPurpose} manifest={manifest} onManifest={setManifest}
        onImported={async bank => { await refresh(); selectBank(`${bank.bank_id}:${bank.version}`) }} onUse={bank => { selectBank(`${bank.bank_id}:${bank.version}`); setStep(0); navigate('create') }} />}
      {view === 'plans' && <section className="evaluation-panel"><EvaluationPlans request={payload} disabled={invalidPlan} jobId={job?.job_id} onLoad={loadPlan} /></section>}
      {view === 'archives' && <section className="evaluation-panel"><EvaluationArchives onOpenDetail={(row, base) => void openDetail(row, base)} /></section>}
      {view === 'create' && <section className="evaluation-editor" aria-label="新建实验配置">
        <div className="evaluation-breadcrumb"><Button type="link" onClick={() => navigate('tasks')}>实验任务</Button><ChevronRight size={14} aria-hidden="true" /><span>新建实验</span></div>
        <h2>{copiedFromJobId ? '从既有方案创建新实验' : '配置对比实验'}</h2>
        <Steps current={step} size="small" onChange={value => value < step && setStep(value)} items={['数据与清单', '对比对象', '实验设置', '校验与创建'].map((title, index) => ({ title, disabled: index > step }))} />
        <div className="evaluation-editor-layout"><div className="evaluation-panel evaluation-editor-main">
            {step === 0 && <><div className="evaluation-form-heading"><h3>选择实验数据</h3><p>固定本次实验使用的数据版本和题目范围</p></div><div className="evaluation-fields">
          <label>任务名称<Input aria-label="任务名称" value={name} maxLength={100} onChange={e => setName(e.target.value)} /></label>
          <label>问题库版本<Select aria-label="问题库版本" value={bankKey} placeholder="选择固定问题库版本"
            options={banks.map(b => ({ value: `${b.bank_id}:${b.version}`, label: `${b.name} · v${b.version} · ${b.count} 题` }))}
            onChange={v => { selectBank(v); setSampleSize(null) }} /></label>
          {selectedBank && <small>有答案 {selectedBank.labelled} 题 · 有逐题材料 {selectedBank.contextual} 题 · 指纹 {selectedBank.sha256.slice(0, 12)}</small>}
          <EvaluationDataPlan bank={selectedBank} purpose={purpose} onPurpose={setPurpose} manifest={manifest} onManifest={setManifest} showManagement={false} />
          {selectedBank && <Button disabled={busy || purpose === 'evaluation'} onClick={() => void action(async () => {
            const result = await evaluationRequest<{ items: QuestionBank['preview'] }>(`/question-banks/${selectedBank.bank_id}/${selectedBank.version}/preview`, 'POST')
            setPreview(result.items)
          })}>预览前三题并登记为已曝光</Button>}
          {preview.length > 0 && <Collapse items={[{ key: 'preview', label: '已登记曝光的预览题目', children: preview.map(q => <p key={q._id}>{q.question}</p>) }]} />}
          <Space wrap>
            <label>评测题数（留空为全部）<InputNumber aria-label="评测题数" disabled={!!manifest} min={1} max={selectedBank?.count ?? 10000} value={manifest?.count ?? sampleSize} onChange={setSampleSize} /></label>
            <label>抽样种子<InputNumber aria-label="抽样种子" disabled={!!manifest} value={seed} precision={0} onChange={value => setSeed(value ?? 42)} /></label>
          </Space>
          <Button type="link" onClick={() => navigate('data')}>管理题库与固定清单</Button>
            </div></>}
            {step === 1 && <>
            <div className="evaluation-form-heading"><h3>选择对比对象</h3><p>组合标准基线与已发布系统版本，每次实验固定所选版本。</p></div>
            <Segmented aria-label="评测对象" value={kind} options={[{ value: 'mixed', label: '全部对象' }, { value: 'builtin', label: '标准基线' }, { value: 'system', label: '我的系统' }]} onChange={value => setKind(value as typeof kind)} />
            {kind !== 'system' && <><div className="evaluation-section-heading"><h3>标准基线</h3><small>可多选</small></div><div className="evaluation-object-grid">{options.methods.map(method => <div key={method} className={`evaluation-object ${selected.includes(method) ? 'selected' : ''}`}>
              <Checkbox checked={selected.includes(method)} disabled={!selected.includes(method) && selected.length >= 12} onChange={event => selectTarget(method, event.target.checked)} aria-label={METHOD_NAMES[method] ?? method}>
              <span><strong>{METHOD_NAMES[method] ?? method}</strong><small>标准对比方法</small></span>
              </Checkbox>
            </div>)}</div></>}
            {kind !== 'builtin' && <><div className="evaluation-section-heading"><h3>我的已发布系统</h3><small>按版本选择</small></div>
              {!options.systems.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="发布系统后即可在此比较" />}
              <div className="evaluation-object-grid">{options.systems.map(system => {
                const value = `${system.system_id}:${system.version}`
                return <div key={value} className={`evaluation-object ${selected.includes(value) ? 'selected' : ''} ${system.available === false ? 'unavailable' : ''}`}>
                  <Checkbox checked={selected.includes(value)} disabled={!selected.includes(value) && (system.available === false || selected.length >= 12)} onChange={event => selectTarget(value, event.target.checked)} aria-label={`${system.name} · ${system.version}`}>
                  <span><strong>{system.name}</strong><small>{system.version} · {system.available === false ? '暂不可用' : '已发布'}</small>{system.available === false && <small>{system.unavailable_reason ?? '请检查系统配置'}</small>}</span>
                  </Checkbox>
                </div>
              })}</div>
            </>}
            <p className="evaluation-hint">已选 {selected.length} 个对象，最多 12 个。{selectedLabels.join('、')}</p>
            {invalidSystemSelection && <Alert type="warning" showIcon title="选中的系统版本已不可用，请取消选择后重新配置。" />}
            </>}
            {step === 2 && <><div className="evaluation-form-heading"><h3>设置实验条件</h3><p>核对常用条件，再按需要展开高级设置。</p></div><div className="evaluation-fields">
          <label>回答材料<Select aria-label="回答材料" value={contextMode} disabled={hasBuiltin} onChange={setContextMode}
            options={[{ value: 'question_context', label: '使用每道题自带的 context' }, { value: 'system_sources', label: '沿用各智能体绑定的固定知识源' }]} /></label>
          {contextMode === 'question_context' && <small>本次运行会用每题 context 替换检索材料，原发布系统保持独立保存。</small>}
          <Space wrap>
            <label>每题重复次数<InputNumber aria-label="重复次数" min={1} max={100} value={repeats} onChange={v => setRepeats(v ?? 1)} /></label>
            {hasBuiltin && <label>检索 top_k<InputNumber aria-label="评测 top_k" min={1} max={50} value={topK} onChange={v => setTopK(v ?? 3)} /></label>}
          </Space>
          <Checkbox checked={override} disabled={hasBuiltin} onChange={e => setOverride(e.target.checked)}>统一模型参数{kind !== 'builtin' ? '（覆盖本次评测副本）' : ''}</Checkbox>
          {override && <><label>模型名称<Input aria-label="评测模型" value={model} onChange={e => setModel(e.target.value)} /></label>
            <Space wrap><label>温度<InputNumber aria-label="评测温度" min={0} max={2} step={0.1} value={temperature} onChange={v => setTemperature(v ?? 0)} /></label>
              <label>输出 Token 上限<InputNumber aria-label="输出 Token 上限" min={1} max={65536} value={maxTokens} onChange={v => setMaxTokens(v ?? 4096)} /></label></Space></>}
          <Collapse items={[{ key: 'advanced', label: '实验与研究统计设置', children: <div className="evaluation-fields">
            <Space wrap>
              <label>研究统计种子<InputNumber aria-label="研究统计种子" value={seed} precision={0} onChange={v => setSeed(v ?? 42)} /></label>
              <label>结构化修正次数<InputNumber aria-label="结构化修正次数" min={0} max={5} value={outputRetries} disabled={!override} onChange={v => setOutputRetries(v ?? 0)} /></label>
              <label>主指标<Select aria-label="主指标" value={primaryMetric} options={['em', 'f1', 'sp_em', 'sp_f1', 'joint_em', 'joint_f1'].map(value => ({ value, label: value.toUpperCase() }))} onChange={setPrimaryMetric} /></label>
              <label>Bootstrap 次数<InputNumber aria-label="Bootstrap 次数" min={100} max={10000} value={bootstrapSamples} onChange={v => setBootstrapSamples(v ?? 2000)} /></label>
              <label>比较基准<Select aria-label="比较基准" allowClear value={referenceIndex ?? undefined} placeholder="优先单智能体，否则首个对象" options={selected.map((value, index) => { const system = options.systems.find(s => `${s.system_id}:${s.version}` === value); return { value: index, label: METHOD_NAMES[value] ?? (system ? `${system.name} · ${system.version}` : value) } })} onChange={v => setReferenceIndex(v ?? null)} /></label>
            </Space>
            <small>固定清单保留原抽样种子和题目顺序；这里的种子用于统计，未选固定清单时也用于任务抽样。</small>
            {hasBuiltin && <>
              <small>以下为标准基线的执行条件；用户系统保留发布版本的流程、检索和资源设置，统一模型勾选仅覆盖模型参数。实际条件请在预检中逐项核对。</small>
              <label>基线专家执行方式<Select aria-label="基线专家执行方式" value={executionMode} options={[{ value: 'sequential', label: '顺序' }, { value: 'parallel', label: '并行' }]} onChange={setExecutionMode} /></label>
              <Space wrap>{([
                ['expert_count', '基线专家数', 2, 3], ['retrieval_retries', '检索重试上限', 0, 3], ['plan_retries', '规划重试上限', 0, 2],
                ['lead_rounds', 'Lead 回合数', 1, 4], ['debate_rounds', 'Debate 回合数', 1, 5],
              ] as const).map(([key, label, min, max]) => <label key={key}>{label}<InputNumber aria-label={label} min={min} max={max} value={advanced[key]} onChange={v => setAdvanced(old => ({ ...old, [key]: v ?? min }))} /></label>)}</Space>
              <Space wrap>{([
                ['max_node_calls', '单题节点上限', 1, 120], ['max_llm_calls', '单题模型调用上限', 1, 160], ['max_reserved_tokens', '单题预留 Token 上限', 1, 4000000],
                ['timeout_seconds', '单题超时（秒）', 1, 1800], ['node_timeout_seconds', '单节点超时（秒）', 1, 180],
              ] as const).map(([key, label, min, max]) => <label key={key}>{label}<InputNumber aria-label={label} min={min} max={max} value={limits[key]} onChange={v => setLimits(old => ({ ...old, [key]: v ?? min }))} /></label>)}</Space>
            </>}
          </div> }]} />
            </div></>}
            {step === 3 && <><div className="evaluation-form-heading"><h3>校验并冻结方案</h3><p>核对数据、系统版本与执行条件，保存为固定实验任务。</p></div>
              <dl className="evaluation-definition"><div><dt>实验名称</dt><dd>{name}</dd></div><div><dt>实验数据</dt><dd>{selectedBank?.name} · v{selectedBank?.version}</dd></div><div><dt>对比对象</dt><dd>{selectedLabels.join('、')}</dd></div>
                <div><dt>模型条件</dt><dd>{override ? model : '沿用各系统发布版本的模型配置'}</dd></div><div><dt>计划问答</dt><dd>{planned.toLocaleString()} 项</dd></div><div><dt>统计口径</dt><dd>首次尝试 · 同题配对 · 主指标 {primaryMetric.toUpperCase()}</dd></div></dl>
              <div className="evaluation-fields evaluation-preflight">
          <Button disabled={invalidPlan} onClick={() => void action(async () => {
            const data = await evaluationRequest<EvaluationPreflight>('/preflight', 'POST', payload)
            setPreflight({ key: payloadKey, data })
          })}>校验方案</Button>
          {purpose === 'evaluation' && !checkedPlan && <small>正式评价需先校验当前方案，再冻结任务；修改数据或参数后需重新校验。</small>}
          {checkedPlan && <div aria-label="已校验实验方案">
            <Alert type="success" title={`校验通过：${checkedPlan.sample_count} 题，${checkedPlan.planned_tasks} 项任务，模型调用 ${checkedPlan.model_calls} 次。`} />
            <p>数据指纹：{checkedPlan.data_sha256}；主指标：{primaryMetric.toUpperCase()}；评分保留首次尝试，失败计零，补跑只作补充。</p>
            <Collapse items={[{ key: 'plan', label: '核对模型、提示词、流程与资源条件', children: checkedPlan.targets.map(t => <section key={t.method_id}>
              <h4>{METHOD_NAMES[t.label] ?? t.label}</h4>
              {t.models.map(m => <p key={m.agent_id}>{m.agent_id}：{m.model?.model_name ?? '检索节点'}；温度 {m.model?.temperature ?? '—'}；输出上限 {m.model?.max_tokens ?? '—'}</p>)}
              <details><summary>完整冻结配置</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(t, null, 2)}</pre></details>
            </section>) }]} />
          </div>}
              </div><p className="evaluation-hint">创建后，在任务详情中单独确认模型调用与费用，再启动后台评测。修改条件后需重新校验。</p>
            </>}
            {planned > 30000 && <Alert type="warning" showIcon title="每个任务最多 30,000 项问答，请减少题数、对象或重复次数。" />}
            <footer className="evaluation-editor-footer"><Button type="text" disabled={invalidPlan} onClick={() => setSavingPlan(true)}>保存为方案</Button><div className="evaluation-button-row">
              <Button disabled={busy} onClick={() => step === 0 ? navigate('tasks') : setStep(step - 1)}>{step === 0 ? '取消' : '上一步'}</Button>
              {step < 3 ? <Button type="primary" disabled={nextDisabled} onClick={() => setStep(step + 1)}>下一步</Button> : <>
          <Button type="primary" disabled={invalidPlan || !checkedPlan}
            onClick={() => void action(async () => {
              const created = await evaluationRequest<EvaluationJob>('/jobs', 'POST', {
                ...payload, preflight_token: checkedPlan?.token ?? null,
              })
              await refresh(); adopt(created)
            })}>冻结并创建任务</Button>              </>}
            </div></footer>
          </div><aside className="evaluation-panel evaluation-editor-summary" aria-label="当前实验概览"><h3>实验概览</h3><dl>
            <div><dt>用途</dt><dd>{EVALUATION_PURPOSE[purpose]}</dd></div><div><dt>题目数量</dt><dd>{manifest?.count ?? sampleSize ?? selectedBank?.count ?? 0} 题</dd></div>
            <div><dt>对比对象</dt><dd>{selected.length} 个</dd></div><div><dt>重复次数</dt><dd>{repeats} 次</dd></div>
          </dl><div className="evaluation-planned-total"><strong>{planned.toLocaleString()}</strong><span>项计划问答</span><p>{manifest?.count ?? sampleSize ?? selectedBank?.count ?? 0} 题 × {selected.length} 个对象 × {repeats} 次</p></div><p className="evaluation-hint">实际模型调用与用量取决于协作过程。</p></aside>
        </div>
      </section>}
      <Modal className="evaluation-console-modal" title="保存实验方案" open={savingPlan} onCancel={() => !busy && setSavingPlan(false)} footer={<div className="evaluation-button-row">
        <Button disabled={busy} onClick={() => setSavingPlan(false)}>取消</Button><Button type="primary" loading={busy} disabled={invalidPlan || !planName.trim()} onClick={() => void action(async () => {
          await evaluationRequest('/plans', 'POST', { name: planName, request: payload }); setSavingPlan(false); setNotice('实验方案已保存，可在“实验方案”中复用。')
        })}>保存方案</Button></div>}>
        <label className="evaluation-modal-field">方案名称<Input aria-label="实验方案名称" value={planName} maxLength={100} onChange={event => setPlanName(event.target.value)} /></label>
      </Modal>
      <Modal className="evaluation-console-modal" title="逐题运行诊断与轨迹" open={detailTarget !== undefined} width={960} style={{ top: 24 }} styles={{ body: { maxHeight: 'calc(100dvh - 156px)', overflowY: 'auto' } }} footer={null} onCancel={closeDetail}>
        {detailLoading && <div role="status" aria-label="正在加载运行详情"><Skeleton active paragraph={{ rows: 6 }} /></div>}
        {detailError && <Alert type="error" showIcon title="运行详情加载失败" description={detailError} action={<Button onClick={() => detailTarget && void openDetail(detailTarget, detailBase.current)}>重新加载详情</Button>} />}
        {detail && <EvaluationRunDetail detail={detail} />}
      </Modal>
    </main>
  </ConfigProvider>
}
