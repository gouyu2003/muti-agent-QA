import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Collapse, Empty, Input, InputNumber, Modal, Progress, Select, Skeleton, Space, Table, Tag, Tooltip } from 'antd'
import { evaluationRequest, type EvaluationJob, type EvaluationOptions, type EvaluationResult, type MethodSummary, type QuestionBank, type EvaluationManifest, type EvaluationPreflight } from './evaluationApi'
import EvaluationDataPlan from './EvaluationDataPlan'
import EvaluationResearch from './EvaluationResearch'
import EvaluationReview from './EvaluationReview'
import EvaluationPlans from './EvaluationPlans'
import EvaluationArchives from './EvaluationArchives'
import { METHOD_NAMES } from './evaluationLabels'
import EvaluationRunDetail from './EvaluationRunDetail'
import type { StoredRunDetail } from './types'

const STATUS: Record<string, string> = { draft: '待启动', queued: '排队中', running: '运行中', stopping: '等待当前题结束', paused: '已暂停', completed: '执行结束', interrupted: '执行中断', error: '执行异常' }
const active = (status?: string) => ['queued', 'running', 'stopping'].includes(status ?? '')
const message = (error: unknown) => error instanceof Error ? error.message : '操作失败'

export default function EvaluationsView() {
  const [banks, setBanks] = useState<QuestionBank[]>([])
  const [options, setOptions] = useState<EvaluationOptions>({ methods: [], systems: [] })
  const [jobs, setJobs] = useState<EvaluationJob[]>([])
  const [job, setJob] = useState<EvaluationJob>()
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [bankName, setBankName] = useState('')
  const [description, setDescription] = useState('')
  const [existingBank, setExistingBank] = useState<string>()
  const [file, setFile] = useState<File>()
  const [bankKey, setBankKey] = useState<string>()
  const [name, setName] = useState('协议批量评测')
  const [kind, setKind] = useState<'builtin' | 'system' | 'mixed'>('builtin')
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
  const [consent, setConsent] = useState(false)
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
  const selectedBank = banks.find(b => `${b.bank_id}:${b.version}` === bankKey)
  const planned = selected.length * (manifest?.count ?? sampleSize ?? selectedBank?.count ?? 0) * repeats
  const isBuiltin = (value: string) => kind === 'builtin' || (kind === 'mixed' && options.methods.includes(value))
  const hasBuiltin = kind === 'builtin' || selected.some(isBuiltin)
  const invalidSystemSelection = kind !== 'builtin' && selected.filter(value => !isBuiltin(value)).some(value => !options.systems.some(
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
    (override && !model.trim()) || (purpose === 'evaluation' && !manifest)
  function selectBank(value: string) { setBankKey(value); setManifest(undefined); setPreview([]) }
  const retryCount = job?.retryable_count ?? 0
  const retryDisabledReason = retryCount === 0
    ? job && job.progress.attempted === job.progress.planned
      ? '暂无可补跑项，所有任务均已完成。'
      : '暂无可补跑项；尚未开始的任务请点击“继续未开始的任务”。'
    : busy ? '正在处理，请稍候。' : !consent ? '请先确认模型调用及费用。' : undefined

  async function refresh() {
    const request = ++refreshRequest.current
    const [b, o, j] = await Promise.all([
      evaluationRequest<{ items: QuestionBank[] }>('/question-banks'),
      evaluationRequest<EvaluationOptions>('/options'),
      evaluationRequest<{ items: EvaluationJob[] }>(showArchived ? '/jobs?include_archived=true' : '/jobs'),
    ])
    if (request !== refreshRequest.current) return
    setBanks(b.items); setOptions(o); setJobs(j.items ?? [])
  }
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError('')
    try { await fn() } catch (e) { setError(message(e)) } finally { setBusy(false) }
  }
  useEffect(() => { refresh().catch(e => setError(message(e))) }, [showArchived])
  useEffect(() => () => { detailRequest.current += 1; refreshRequest.current += 1 }, [])
  useEffect(() => {
    if (!job) return
    let disposed = false
    async function load() {
      try {
        const current = await evaluationRequest<EvaluationJob>(`/jobs/${job!.job_id}`)
        if (!disposed) {
          setJob(current)
          setJobs(previous => previous.map(j => j.job_id === current.job_id ? current : j))
        }
      } catch (e) { if (!disposed) setError(message(e)) }
    }
    void load()
    const timer = window.setInterval(() => void load(), 3000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [job?.job_id])

  function adopt(item: EvaluationJob) {
    closeDetail(); setJob(item); setConsent(false); setNotice('')
  }

  function closeDetail() {
    detailRequest.current += 1
    setDetailTarget(undefined); setDetail(undefined); setDetailError(''); setDetailLoading(false)
  }

  async function openDetail(row: EvaluationResult, sourceBase?: string) {
    const base = sourceBase ?? (job ? `/jobs/${job.job_id}` : undefined)
    if (!base) return
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
    setPreflight(undefined); setConsent(false); closeDetail(); setJob(undefined)
    setError(!banks.some(b => b.bank_id === request.bank_id && b.version === request.bank_version) || (request.manifest_id && !selectedManifest)
      ? '源方案绑定的题库或清单在当前环境不可用，请重新选择并校验。' : '')
    setNotice('已载入新实验配置。请核对当前数据与版本并重新校验、冻结；原任务与原成绩保持不变。')
  }
  const visibleJobs = jobs.filter(j => (!jobSearch || `${j.name} ${j.job_id}`.toLowerCase().includes(jobSearch.toLowerCase())) &&
    (!jobStatus || j.status === jobStatus) && (!jobPurpose || (j.plan?.request.purpose ?? 'legacy') === jobPurpose))

  return <main className="evaluations-page">
    <div className="evaluations-heading"><div><h1>批量评测</h1><p>保存问题库与待测版本，提交后台任务，比较回答质量、开销和失败情况。</p></div>
      <Button disabled={busy} onClick={() => void action(refresh)}>刷新列表</Button></div>
    {error && <Alert type="error" showIcon title={error} closable onClose={() => setError('')} />}
    {notice && <Alert type="info" title={notice} closable onClose={() => setNotice('')} />}
    <div className="evaluation-setup">
      <Card title="1. 导入问题库">
        <div className="evaluation-fields">
          <label>问题库名称<Input aria-label="问题库名称" value={bankName} maxLength={100} onChange={e => setBankName(e.target.value)} /></label>
          <label>说明<Input.TextArea aria-label="问题库说明" value={description} maxLength={2000} onChange={e => setDescription(e.target.value)} /></label>
          <label>保存位置<Select aria-label="问题库保存位置" allowClear placeholder="创建新问题库" value={existingBank}
            options={[...new Map(banks.map(b => [b.bank_id, { value: b.bank_id, label: b.name }])).values()]}
            onChange={setExistingBank} /></label>
          <label>问题 JSON 文件<input aria-label="问题 JSON 文件" type="file" accept=".json,application/json" onChange={e => setFile(e.target.files?.[0])} /></label>
          <small>支持 HotpotQA JSON 数组，或 {`[{"id":"q1","question":"…","answer":"…"}]`}。answer 可省略；有标准答案才计算 EM/F1。新增版本保留旧内容。</small>
          <Button disabled={busy || !bankName.trim() || !file} onClick={() => void action(async () => {
            const form = new FormData(); form.append('name', bankName); form.append('description', description); form.append('file', file!)
            if (existingBank) form.append('bank_id', existingBank)
            const bank = await evaluationRequest<QuestionBank>('/question-banks', 'POST', form)
            await refresh(); selectBank(`${bank.bank_id}:${bank.version}`)
          })}>导入并保存固定版本</Button>
          {banks.length > 0 && <><h3>已保存的问题库版本</h3><div className="evaluation-bank-list">{banks.slice(0, 8).map(b =>
            <button type="button" key={`${b.bank_id}:${b.version}`} className={bankKey === `${b.bank_id}:${b.version}` ? 'selected' : ''}
              onClick={() => { selectBank(`${b.bank_id}:${b.version}`); setSampleSize(null) }}>
              <strong>{b.name} · v{b.version}</strong><span>{b.count} 题 · 有答案 {b.labelled} 题</span>
            </button>)}</div></>}
        </div>
      </Card>
      <Card title="2. 配置评测任务">
        <div className="evaluation-fields">
          <label>任务名称<Input aria-label="任务名称" value={name} maxLength={100} onChange={e => setName(e.target.value)} /></label>
          <label>问题库版本<Select aria-label="问题库版本" value={bankKey} placeholder="选择固定问题库版本"
            options={banks.map(b => ({ value: `${b.bank_id}:${b.version}`, label: `${b.name} · v${b.version} · ${b.count} 题` }))}
            onChange={v => { selectBank(v); setSampleSize(null) }} /></label>
          {selectedBank && <small>有答案 {selectedBank.labelled} 题 · 有逐题材料 {selectedBank.contextual} 题 · 指纹 {selectedBank.sha256.slice(0, 12)}</small>}
          <EvaluationDataPlan bank={selectedBank} purpose={purpose} onPurpose={setPurpose} manifest={manifest} onManifest={setManifest} />
          {selectedBank && <Button disabled={busy || purpose === 'evaluation'} onClick={() => void action(async () => {
            const result = await evaluationRequest<{ items: QuestionBank['preview'] }>(`/question-banks/${selectedBank.bank_id}/${selectedBank.version}/preview`, 'POST')
            setPreview(result.items)
          })}>预览前三题并登记为已曝光</Button>}
          {preview.length > 0 && <Collapse items={[{ key: 'preview', label: '已登记曝光的预览题目', children: preview.map(q => <p key={q._id}>{q.question}</p>) }]} />}
          <label>评测对象<Select aria-label="评测对象" value={kind} options={[{ value: 'builtin', label: '标准六组基线' }, { value: 'system', label: '我的系统：已发布版本' }, { value: 'mixed', label: '混合比较：基线＋我的系统' }]}
            onChange={v => { setKind(v); setSelected([]); setReferenceIndex(null); setOverride(v !== 'system'); setContextMode(v !== 'system' ? 'question_context' : 'system_sources') }} /></label>
          <label>待比较方法<Select aria-label="待比较方法" mode="multiple" value={selected} maxCount={12} placeholder="选择 1～12 项"
            options={[
              ...(kind !== 'system' ? options.methods.map(m => ({ value: m, label: METHOD_NAMES[m] ?? m })) : []),
              ...(kind !== 'builtin' ? options.systems.map(s => ({ value: `${s.system_id}:${s.version}`, label: `${s.name} · ${s.version}${s.available === false ? '（暂不可用）' : ''}`,
                disabled: s.available === false, title: s.unavailable_reason ?? undefined })) : []),
            ]}
            onChange={values => { setSelected(values); setReferenceIndex(null); if (kind !== 'system' && values.some(v => options.methods.includes(v))) { setContextMode('question_context'); setOverride(true) } }} /></label>
          {kind !== 'builtin' && <small>可比较同一系统的不同发布版本，也可比较不同系统。混合基线时统一使用每题 context；每次评测固定所选版本。</small>}
          {kind !== 'builtin' && options.systems.filter(s => s.available === false).map(s =>
            <small key={`${s.system_id}:${s.version}`}>{s.name} · {s.version}：{s.unavailable_reason ?? '此版本暂不可用，请检查系统配置。'}</small>)}
          <label>回答材料<Select aria-label="回答材料" value={contextMode} disabled={hasBuiltin} onChange={setContextMode}
            options={[{ value: 'question_context', label: '使用每道题自带的 context' }, { value: 'system_sources', label: '沿用各智能体绑定的固定知识源' }]} /></label>
          {contextMode === 'question_context' && <small>本次运行会用每题 context 替换检索材料，原发布系统保持独立保存。</small>}
          <Space wrap>
            <label>题数（留空为全部）<InputNumber aria-label="评测题数" disabled={!!manifest} min={1} max={selectedBank?.count ?? 10000} value={sampleSize} onChange={setSampleSize} /></label>
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
          <p>计划 {planned.toLocaleString()} 次完整问答（每任务最多 30,000 次）；实际模型请求数和费用取决于协作过程。</p>
          <Button disabled={invalidPlan} onClick={() => void action(async () => {
            const data = await evaluationRequest<EvaluationPreflight>('/preflight', 'POST', payload)
            setPreflight({ key: payloadKey, data })
          })}>启动前校验与方案预览（不调用模型）</Button>
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
          <Button type="primary" disabled={invalidPlan || (purpose === 'evaluation' && !checkedPlan)}
            onClick={() => void action(async () => {
              const created = await evaluationRequest<EvaluationJob>('/jobs', 'POST', {
                ...payload, preflight_token: checkedPlan?.token ?? null,
              })
              await refresh(); adopt(created)
            })}>冻结并创建任务（不调用模型）</Button>
        </div>
      </Card>
    </div>
    <Collapse items={[{ key: 'plans', label: '保存与复用实验方案', children: <EvaluationPlans request={payload} disabled={invalidPlan} jobId={job?.job_id} onLoad={loadPlan} /> },
      { key: 'archives', label: '旧 CLI 只读实验档案', children: <EvaluationArchives onOpenDetail={(row, base) => void openDetail(row, base)} /> }]} />
    <div className="evaluation-workspace">
      <Card title="评测任务">
        <Space wrap>
          <Input aria-label="搜索实验任务" placeholder="按任务名称或 ID 搜索" value={jobSearch} onChange={e => setJobSearch(e.target.value)} />
          <Select aria-label="任务状态筛选" allowClear placeholder="全部状态" value={jobStatus} onChange={setJobStatus} options={Object.entries(STATUS).map(([value, label]) => ({ value, label }))} />
          <Select aria-label="任务用途筛选" allowClear placeholder="全部用途" value={jobPurpose} onChange={setJobPurpose} options={[
            { value: 'smoke', label: '试跑' }, { value: 'development', label: '开发' }, { value: 'evaluation', label: '正式评价' }, { value: 'legacy', label: '历史未标用途' },
          ]} />
          <Checkbox checked={showArchived} onChange={e => setShowArchived(e.target.checked)}>显示已归档任务</Checkbox>
        </Space>
        {!visibleJobs.length ? <Empty description="没有符合条件的任务" /> : <div className="evaluation-job-list">{visibleJobs.map(j => <button type="button" key={j.job_id}
          className={j.job_id === job?.job_id ? 'selected' : ''} onClick={() => adopt(j)}><strong>{j.name}</strong>
          <span>{j.archived ? '已归档 · ' : ''}{STATUS[j.status] ?? j.status} · {j.progress.attempted}/{j.progress.planned}</span></button>)}</div>}
      </Card>
      <Card title={job ? job.name : '任务详情'}>
        {!job ? <Empty description="选择任务，检查冻结配置后启动" /> : <>
          <Space wrap><Tag>{STATUS[job.status] ?? job.status}</Tag><span>{job.sample_count} 题 × {job.targets.length} 项 × {job.repeats} 次</span></Space>
          <p>问题库：{job.bank.name} · v{job.bank.version}；材料：{job.context_mode === 'question_context' ? '每题 context' : '系统固定知识源'}</p>
          <p>评测对象：{job.targets.map(t => METHOD_NAMES[t.label] ?? t.label).join('、')}</p>
          <small>冻结指纹：{job.fingerprint}</small>
          <Progress percent={Math.round(100 * (job.progress.completed + job.progress.failed + job.progress.interrupted + (job.progress.cancelled ?? 0)) / job.progress.planned)} />
          <p>完成 {job.progress.completed} · 失败 {job.progress.failed} · 状态不明 {job.progress.interrupted}；进度按首次尝试统计。</p>
          {job.error && <Alert type="error" title={job.error} />}
          <Space wrap>
            <Button disabled={busy} onClick={() => void action(async () => {
              const source = await evaluationRequest<{ request: Record<string, unknown> }>(`/jobs/${job.job_id}/copy-plan`, 'POST')
              await loadPlan(source.request)
            })}>复制为新实验配置</Button>
            <Button disabled={busy || active(job.status)} onClick={() => void action(async () => {
              setJob(await evaluationRequest<EvaluationJob>(`/jobs/${job.job_id}/archive`, 'POST', { archived: !job.archived })); await refresh()
            })}>{job.archived ? '取消归档' : '归档任务（保留结果）'}</Button>
          </Space>
          {job.plan && <Collapse items={[{ key: 'frozen-plan', label: '查看本任务冻结方案与题目清单', children:
            <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(job.plan, null, 2)}</pre> }]} />}
          {job.archived ? <Alert type="info" title="此任务已归档，结果保留；取消归档后才可继续或补跑。" /> : !active(job.status) ? <>
            <Checkbox checked={consent} onChange={e => setConsent(e.target.checked)}>我确认启动会调用真实模型并产生费用</Checkbox>
            <Space wrap className="evaluation-actions">{(job.status === 'draft' || job.progress.attempted < job.progress.planned) && <Button type="primary" disabled={busy || !consent} onClick={() => void action(async () => {
              setJob(await evaluationRequest<EvaluationJob>(`/jobs/${job.job_id}/start`, 'POST', { allow_model_calls: true })); setConsent(false)
            })}>{job.status === 'draft' ? '启动后台评测' : '继续未开始的任务'}</Button>}
              {job.status !== 'draft' && <Tooltip title={retryDisabledReason} trigger={['hover', 'focus']}>
                <span tabIndex={retryDisabledReason ? 0 : undefined} style={{ display: 'inline-block' }}>
                  <Button disabled={!!retryDisabledReason} onClick={() => void action(async () => {
                    setJob(await evaluationRequest<EvaluationJob>(`/jobs/${job.job_id}/start`, 'POST', { allow_model_calls: true, retry_failed: true })); setConsent(false)
                  })}>补跑失败／状态不明项{retryCount > 0 ? `（${retryCount}）` : ''}</Button>
                </span>
              </Tooltip>}</Space>
          </> : <Button disabled={busy || job.status === 'stopping'} onClick={() => void action(async () => setJob(await evaluationRequest<EvaluationJob>(`/jobs/${job.job_id}/stop`, 'POST')))}>暂停（当前题结束后生效）</Button>}
          <p>后台执行与页面独立。关闭页面后任务继续；重启电脑会中断，恢复时核对终态。补跑另存尝试，主表保留首次成绩。</p>
          {job.report && <>
            <Alert type={job.report.complete ? 'info' : 'warning'} title={job.report.complete ? '首次尝试已全部结束，失败项计零；完成不等于答案正确。' : '首次尝试尚不完整，主表不显示完整集成绩。'} />
            <Table<MethodSummary & { method: string }> size="small" pagination={false} scroll={{ x: 1000 }} rowKey="method" dataSource={Object.entries(job.report.methods).map(([method, data]) => ({ method, ...data }))}
              columns={[{ title: '方法', dataIndex: 'method', render: m => METHOD_NAMES[m] ?? job.targets.find(t => t.method_id === m)?.label ?? m },
                { title: '完成 / 失败', key: 'status', render: (_, r) => `${r.statuses.completed} / ${r.statuses.failed}` },
                ...['em', 'f1', 'sp_f1', 'joint_f1'].map(metric => ({ title: metric.toUpperCase(), key: metric, render: (_: unknown, r: MethodSummary) => r.scores[metric]?.value == null ? '—' : `${(100 * r.scores[metric].value!).toFixed(1)}% (${r.scores[metric].scored})` })),
                { title: '已知 Token', key: 'tokens', render: (_: unknown, r: MethodSummary) => r.usage.total_tokens.reported_total?.toLocaleString() ?? '未知' },
                { title: '调用次数', key: 'calls', render: (_, r) => r.usage.llm_call_count.reported_total ?? '未知' },
                { title: '平均耗时（秒）', key: 'duration', render: (_, r) => r.usage.duration_ms.mean_reported == null ? '未知' : (r.usage.duration_ms.mean_reported / 1000).toFixed(1) }]} />
            <Table size="small" pagination={false} rowKey="method" dataSource={Object.entries(job.report.methods).map(([method, data]) => ({ method, ...data }))} columns={[
              { title: '开销分布', dataIndex: 'method', render: m => METHOD_NAMES[m] ?? job.targets.find(t => t.method_id === m)?.label ?? m },
              { title: '耗时中位数（秒）', render: (_, r) => r.usage.duration_ms.median_reported == null ? '未知' : (r.usage.duration_ms.median_reported / 1000).toFixed(1) },
              { title: '耗时 P95（秒）', render: (_, r) => r.usage.duration_ms.p95_reported == null ? '未知' : (r.usage.duration_ms.p95_reported / 1000).toFixed(1) },
              { title: '已知输入／输出 Token', render: (_, r) => `${r.usage.prompt_tokens?.reported_total ?? '未知'} / ${r.usage.completion_tokens?.reported_total ?? '未知'}` },
            ]} scroll={{ x: 700 }} />
            <small>括号为该指标的已评分题数；无标准答案不评分。EM/F1 使用英文 HotpotQA 归一化规则。已知 Token 合计不包含未知用量。</small>
          </>}
          <EvaluationResearch key={job.job_id} job={job} />
          <Space className="evaluation-actions"><a href={`/api/v1/evaluations/jobs/${job.job_id}/export?format=csv`}>导出逐题 CSV／人工审阅表</a><a href={`/api/v1/evaluations/jobs/${job.job_id}/export?format=json`}>导出报告 JSON</a></Space>
          <EvaluationReview key={job.job_id} job={job} base={`/jobs/${job.job_id}`} onOpenDetail={row => void openDetail(row)} />
          <Collapse items={[{ key: 'rules', label: '本次评测的边界', children: <p>每个平台同时执行一个任务。原系统版本、问题库版本与模型配置随任务冻结；开始后修改源码或依赖会阻止恢复，请新建任务。平台外人工接触和开发历史需要显式登记。复制方案生成新任务，不续接或覆盖旧实验。</p> }]} />
        </>}
      </Card>
    </div>
    <Modal title="逐题运行诊断与轨迹" open={detailTarget !== undefined} width={960} style={{ top: 24 }}
      styles={{ body: { maxHeight: 'calc(100dvh - 156px)', overflowY: 'auto' } }} footer={null} onCancel={closeDetail}>
      {detailLoading && <div role="status" aria-label="正在加载运行详情"><Skeleton active paragraph={{ rows: 6 }} /></div>}
      {detailError && <Alert type="error" showIcon title="运行详情加载失败" description={detailError}
        action={<Button onClick={() => detailTarget && void openDetail(detailTarget)}>重新加载详情</Button>} />}
      {detail && <EvaluationRunDetail detail={detail} />}
    </Modal>
  </main>
}
