import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Collapse, Empty, Input, InputNumber, Modal, Progress, Select, Skeleton, Space, Table, Tag } from 'antd'
import { evaluationRequest, type EvaluationJob, type EvaluationOptions, type EvaluationResult, type MethodSummary, type QuestionBank } from './evaluationApi'
import EvaluationRunDetail from './EvaluationRunDetail'
import { runDiagnosticSummary } from './diagnosticSummary'
import type { StoredRunDetail } from './types'

const METHOD_NAMES: Record<string, string> = { single_rag: '单智能体 RAG', pipeline_fixed: '固定流水线', pipeline_adaptive: '反馈流水线', multi_expert: '多专家', lead: 'Lead Agent', debate: 'Debate' }
const STATUS: Record<string, string> = { draft: '待启动', queued: '排队中', running: '运行中', stopping: '等待当前题结束', paused: '已暂停', completed: '执行结束', interrupted: '执行中断', error: '执行异常' }
const active = (status?: string) => ['queued', 'running', 'stopping'].includes(status ?? '')
const message = (error: unknown) => error instanceof Error ? error.message : '操作失败'

export default function EvaluationsView() {
  const [banks, setBanks] = useState<QuestionBank[]>([])
  const [options, setOptions] = useState<EvaluationOptions>({ methods: [], systems: [] })
  const [jobs, setJobs] = useState<EvaluationJob[]>([])
  const [job, setJob] = useState<EvaluationJob>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [bankName, setBankName] = useState('')
  const [description, setDescription] = useState('')
  const [existingBank, setExistingBank] = useState<string>()
  const [file, setFile] = useState<File>()
  const [bankKey, setBankKey] = useState<string>()
  const [name, setName] = useState('协议批量评测')
  const [kind, setKind] = useState<'builtin' | 'system'>('builtin')
  const [selected, setSelected] = useState<string[]>([])
  const [contextMode, setContextMode] = useState('question_context')
  const [override, setOverride] = useState(true)
  const [model, setModel] = useState('deepseek-v4.1-flash')
  const [temperature, setTemperature] = useState(0)
  const [maxTokens, setMaxTokens] = useState(4096)
  const [topK, setTopK] = useState(3)
  const [repeats, setRepeats] = useState(1)
  const [sampleSize, setSampleSize] = useState<number | null>(null)
  const [consent, setConsent] = useState(false)
  const [rows, setRows] = useState<EvaluationResult[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [detail, setDetail] = useState<StoredRunDetail>()
  const [detailTarget, setDetailTarget] = useState<EvaluationResult>()
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const detailRequest = useRef(0)
  const selectedBank = banks.find(b => `${b.bank_id}:${b.version}` === bankKey)
  const planned = selected.length * (sampleSize ?? selectedBank?.count ?? 0) * repeats

  async function refresh() {
    const [b, o, j] = await Promise.all([
      evaluationRequest<{ items: QuestionBank[] }>('/question-banks'),
      evaluationRequest<EvaluationOptions>('/options'),
      evaluationRequest<{ items: EvaluationJob[] }>('/jobs'),
    ])
    setBanks(b.items); setOptions(o); setJobs(j.items)
  }
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError('')
    try { await fn() } catch (e) { setError(message(e)) } finally { setBusy(false) }
  }
  useEffect(() => { refresh().catch(e => setError(message(e))) }, [])
  useEffect(() => () => { detailRequest.current += 1 }, [])
  useEffect(() => {
    if (!job) return
    let disposed = false
    async function load() {
      try {
        const [current, result] = await Promise.all([
          evaluationRequest<EvaluationJob>(`/jobs/${job!.job_id}`),
          evaluationRequest<{ items: EvaluationResult[]; total: number }>(`/jobs/${job!.job_id}/results?offset=${(page - 1) * 20}&limit=20`),
        ])
        if (!disposed) {
          setJob(current); setRows(result.items); setTotal(result.total)
          setJobs(previous => previous.map(j => j.job_id === current.job_id ? current : j))
        }
      } catch (e) { if (!disposed) setError(message(e)) }
    }
    void load()
    const timer = window.setInterval(() => void load(), 3000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [job?.job_id, page])

  function adopt(item: EvaluationJob) {
    closeDetail(); setJob(item); setPage(1); setRows([]); setTotal(0); setConsent(false)
  }

  function closeDetail() {
    detailRequest.current += 1
    setDetailTarget(undefined); setDetail(undefined); setDetailError(''); setDetailLoading(false)
  }

  async function openDetail(row: EvaluationResult) {
    if (!job) return
    const request = ++detailRequest.current
    setDetailTarget(row); setDetail(undefined); setDetailError(''); setDetailLoading(true)
    try {
      const result = await evaluationRequest<StoredRunDetail>(`/jobs/${job.job_id}/runs/${row.run_id}`)
      if (request === detailRequest.current) setDetail(result)
    } catch (error) {
      if (request === detailRequest.current) setDetailError(message(error))
    } finally {
      if (request === detailRequest.current) setDetailLoading(false)
    }
  }

  return <main className="evaluations-page">
    <div className="evaluations-heading"><div><h1>批量评测</h1><p>保存问题库与待测版本，提交后台任务，比较回答质量、开销和失败情况。</p></div>
      <Button disabled={busy} onClick={() => void action(refresh)}>刷新列表</Button></div>
    {error && <Alert type="error" showIcon title={error} closable onClose={() => setError('')} />}
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
            await refresh(); setBankKey(`${bank.bank_id}:${bank.version}`)
          })}>导入并保存固定版本</Button>
          {banks.length > 0 && <><h3>已保存的问题库版本</h3><div className="evaluation-bank-list">{banks.slice(0, 8).map(b =>
            <button type="button" key={`${b.bank_id}:${b.version}`} className={bankKey === `${b.bank_id}:${b.version}` ? 'selected' : ''}
              onClick={() => { setBankKey(`${b.bank_id}:${b.version}`); setSampleSize(null) }}>
              <strong>{b.name} · v{b.version}</strong><span>{b.count} 题 · 有答案 {b.labelled} 题</span>
            </button>)}</div></>}
        </div>
      </Card>
      <Card title="2. 配置评测任务">
        <div className="evaluation-fields">
          <label>任务名称<Input aria-label="任务名称" value={name} maxLength={100} onChange={e => setName(e.target.value)} /></label>
          <label>问题库版本<Select aria-label="问题库版本" value={bankKey} placeholder="选择固定问题库版本"
            options={banks.map(b => ({ value: `${b.bank_id}:${b.version}`, label: `${b.name} · v${b.version} · ${b.count} 题` }))}
            onChange={v => { setBankKey(v); setSampleSize(null) }} /></label>
          {selectedBank && <small>有答案 {selectedBank.labelled} 题 · 有逐题材料 {selectedBank.contextual} 题 · 指纹 {selectedBank.sha256.slice(0, 12)}</small>}
          {selectedBank && <Collapse items={[{ key: 'preview', label: '预览前三道问题', children: selectedBank.preview.map(q => <p key={q._id}>{q.question}</p>) }]} />}
          <label>评测对象<Select aria-label="评测对象" value={kind} options={[{ value: 'builtin', label: '标准六组基线' }, { value: 'system', label: '我的系统：已发布版本' }]}
            onChange={v => { setKind(v); setSelected([]); setOverride(v === 'builtin'); setContextMode(v === 'builtin' ? 'question_context' : 'system_sources') }} /></label>
          <label>待比较方法<Select aria-label="待比较方法" mode="multiple" value={selected} maxCount={6} placeholder="选择 1～6 项"
            options={kind === 'builtin' ? options.methods.map(m => ({ value: m, label: METHOD_NAMES[m] ?? m })) :
              options.systems.map(s => ({ value: `${s.system_id}:${s.version}`, label: `${s.name} · ${s.version}` }))}
            onChange={setSelected} /></label>
          <label>回答材料<Select aria-label="回答材料" value={contextMode} disabled={kind === 'builtin'} onChange={setContextMode}
            options={[{ value: 'question_context', label: '使用每道题自带的 context' }, { value: 'system_sources', label: '沿用各智能体绑定的固定知识源' }]} /></label>
          {contextMode === 'question_context' && <small>本次运行会用每题 context 替换检索材料，原发布系统保持独立保存。</small>}
          <Space wrap>
            <label>题数（留空为全部）<InputNumber aria-label="评测题数" min={1} max={selectedBank?.count ?? 10000} value={sampleSize} onChange={setSampleSize} /></label>
            <label>每题重复次数<InputNumber aria-label="重复次数" min={1} max={3} value={repeats} onChange={v => setRepeats(v ?? 1)} /></label>
            {kind === 'builtin' && <label>检索 top_k<InputNumber aria-label="评测 top_k" min={1} max={50} value={topK} onChange={v => setTopK(v ?? 3)} /></label>}
          </Space>
          <Checkbox checked={override} disabled={kind === 'builtin'} onChange={e => setOverride(e.target.checked)}>统一模型参数{kind === 'system' ? '（覆盖本次评测副本）' : ''}</Checkbox>
          {override && <><label>模型名称<Input aria-label="评测模型" value={model} onChange={e => setModel(e.target.value)} /></label>
            <Space wrap><label>温度<InputNumber aria-label="评测温度" min={0} max={2} step={0.1} value={temperature} onChange={v => setTemperature(v ?? 0)} /></label>
              <label>输出 Token 上限<InputNumber aria-label="输出 Token 上限" min={1} max={65536} value={maxTokens} onChange={v => setMaxTokens(v ?? 4096)} /></label></Space></>}
          <p>计划 {planned.toLocaleString()} 次完整问答；实际模型请求数和费用取决于协作过程。</p>
          <Button type="primary" disabled={busy || !selectedBank || !selected.length || !name.trim() || (override && !model.trim())}
            onClick={() => void action(async () => {
              const targets = selected.map(value => kind === 'builtin' ? { kind, method: value } : {
                kind, system_id: options.systems.find(s => `${s.system_id}:${s.version}` === value)!.system_id,
                version: options.systems.find(s => `${s.system_id}:${s.version}` === value)!.version,
              })
              const created = await evaluationRequest<EvaluationJob>('/jobs', 'POST', {
                name, bank_id: selectedBank!.bank_id, bank_version: selectedBank!.version, targets, context_mode: contextMode,
                sample_size: sampleSize, repeats, top_k: topK,
                model: override ? { name: model, temperature, max_tokens: maxTokens, output_retries: 2 } : null,
              })
              await refresh(); adopt(created)
            })}>冻结并创建任务（不调用模型）</Button>
        </div>
      </Card>
    </div>
    <div className="evaluation-workspace">
      <Card title="评测任务">
        {!jobs.length ? <Empty description="尚无任务" /> : <div className="evaluation-job-list">{jobs.map(j => <button type="button" key={j.job_id}
          className={j.job_id === job?.job_id ? 'selected' : ''} onClick={() => adopt(j)}><strong>{j.name}</strong>
          <span>{STATUS[j.status] ?? j.status} · {j.progress.attempted}/{j.progress.planned}</span></button>)}</div>}
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
          {!active(job.status) ? <>
            <Checkbox checked={consent} onChange={e => setConsent(e.target.checked)}>我确认启动会调用真实模型并产生费用</Checkbox>
            <Space wrap className="evaluation-actions">{(job.status === 'draft' || job.progress.attempted < job.progress.planned) && <Button type="primary" disabled={busy || !consent} onClick={() => void action(async () => {
              setJob(await evaluationRequest<EvaluationJob>(`/jobs/${job.job_id}/start`, 'POST', { allow_model_calls: true })); setConsent(false)
            })}>{job.status === 'draft' ? '启动后台评测' : '继续未开始的任务'}</Button>}
              {job.status !== 'draft' && <Button disabled={busy || !consent} onClick={() => void action(async () => {
                setJob(await evaluationRequest<EvaluationJob>(`/jobs/${job.job_id}/start`, 'POST', { allow_model_calls: true, retry_failed: true })); setConsent(false)
              })}>补跑失败／状态不明项</Button>}</Space>
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
            <small>括号为该指标的已评分题数；无标准答案不评分。EM/F1 使用英文 HotpotQA 归一化规则。已知 Token 合计不包含未知用量。</small>
          </>}
          <Space className="evaluation-actions"><a href={`/api/v1/evaluations/jobs/${job.job_id}/export?format=csv`}>导出逐题 CSV／人工审阅表</a><a href={`/api/v1/evaluations/jobs/${job.job_id}/export?format=json`}>导出报告 JSON</a></Space>
          <Table size="small" rowKey="run_id" dataSource={rows} scroll={{ x: 700 }} pagination={{ current: page, total, pageSize: 20, onChange: setPage }}
            columns={[{ title: '问题', dataIndex: 'question', ellipsis: true }, { title: '方法', dataIndex: 'method_id', render: m => METHOD_NAMES[m] ?? job.targets.find(t => t.method_id === m)?.label ?? m },
              { title: '重复', dataIndex: 'repeat', render: r => r + 1 }, { title: '尝试', dataIndex: 'attempt' }, { title: '状态', dataIndex: 'status' }, { title: '答案', dataIndex: 'answer', ellipsis: true },
              { title: '运行诊断', key: 'error', render: (_, r) => {
                const summary = runDiagnosticSummary(r)
                return summary.hasIssues ? <div className="evaluation-diagnostic-cell"><Tag color={summary.color}>{summary.heading}</Tag>
                  <span>{summary.labels.join('、') || r.error?.message || '查看详情了解执行记录'}</span></div> : '—'
              } },
              { title: '详情', key: 'trace', render: (_, r) => <Button size="small" onClick={() => void openDetail(r)}>查看诊断与轨迹</Button> }]} />
          <Collapse items={[{ key: 'rules', label: '本次评测的边界', children: <p>首版每个平台同时执行一个任务。原系统版本、问题库版本与模型配置随任务冻结；开始后修改源码或依赖会阻止恢复，请新建任务。平台问题库的抽样不替代论文实验的数据曝光登记与独立评价划分。</p> }]} />
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
