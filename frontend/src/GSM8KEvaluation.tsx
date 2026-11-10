import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Checkbox, Collapse, Input, InputNumber, Modal, Select, Space } from 'antd'
import { evaluationRequest } from './evaluationApi'

type Method = 'single' | 'reflection' | 'majority' | 'debate'
type Score = { correct: number | null; prediction?: string | null; parse_failure?: boolean }
type Result = { qid: string; method: Method; repeat: number; status: string; question: string; gold: string;
  outputs: string[]; scores: Record<string, Score>; error?: string; duration_ms?: number }
type Call = { task_key: string; agent: number; round: number; attempt: number; status: string; duration_ms: number | null;
  request: { messages?: { role: string; content: string }[] }; response: { content?: string; finish_reason?: string; usage?: unknown } | null; error: unknown }
type Job = { job_id: string; name: string; state: string; error: string | null; fake: boolean; model: string;
  methods: Method[]; selection: string; samples: number; planned_calls: number; actual_calls: number;
  counts: Record<string, number>; results: Result[]; calls?: Call[]; profile_sha256: string; budget_cny: string;
  pricing: { input_per_million_cny: string; output_per_million_cny: string; verified_at: string; source: string };
  report: { usage: Record<string, number | null>; unknown_requests: number } | null }

const labels: Record<Method, string> = { single: 'Single', reflection: 'Reflection', majority: 'Majority', debate: 'Debate' }
const callsPerMethod: Record<Method, number> = { single: 1, reflection: 2, majority: 6, debate: 6 }
const states: Record<string, string> = { draft: '待启动', running: '运行中', stopping: '当前题完成后暂停', paused: '已暂停', completed: '已完成', interrupted: '已中断' }

export default function GSM8KEvaluation() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [job, setJob] = useState<Job>()
  const [name, setName] = useState('GSM8K 训练题复现')
  const [selection, setSelection] = useState('debug')
  const [count, setCount] = useState(1)
  const [methods, setMethods] = useState<Method[]>(['single', 'reflection', 'majority', 'debate'])
  const [custom, setCustom] = useState('')
  const [fake, setFake] = useState(false)
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string>()

  const refreshList = useCallback(async () => {
    const data = await evaluationRequest<{ items: Job[] }>('/gsm8k/jobs')
    setJobs(data.items)
  }, [])
  const refreshJob = useCallback(async (id: string) => {
    const data = await evaluationRequest<Job>(`/gsm8k/jobs/${id}`)
    setJob(data)
    setJobs(previous => previous.map(item => item.job_id === id ? data : item))
  }, [])
  useEffect(() => { void refreshList().catch(e => setError(e instanceof Error ? e.message : '读取失败')) }, [refreshList])
  useEffect(() => {
    if (!job || !['running', 'stopping'].includes(job.state)) return
    const timer = window.setInterval(() => { void refreshJob(job.job_id).catch(e => setError(e instanceof Error ? e.message : '读取失败')) }, 2000)
    return () => window.clearInterval(timer)
  }, [job?.job_id, job?.state, refreshJob])
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError('')
    try { await fn() } catch (e) { setError(e instanceof Error ? e.message : '操作失败') } finally { setBusy(false) }
  }
  async function create() {
    let records: unknown = null
    if (selection === 'custom') {
      try { records = JSON.parse(custom) } catch { throw new Error('题目须为 JSON 数组，每项包含 question 和 answer。') }
      if (!Array.isArray(records) || records.length !== count) throw new Error('自定义题目数量需与题数一致。')
    }
    const data = await evaluationRequest<Job>('/gsm8k/jobs', 'POST', { name, methods, selection, count, records, fake })
    await refreshList(); await refreshJob(data.job_id); setSelected(undefined)
  }
  async function importFile(file: File) {
    if (file.size > 1024 * 1024) throw new Error('题目文件不能超过 1 MiB。')
    let records: unknown
    try { records = JSON.parse(await file.text()) } catch { throw new Error('题目文件必须是 UTF-8 编码的 JSON 数组。') }
    if (!Array.isArray(records) || records.length < 1 || records.length > 100) throw new Error('题目文件需包含 1～100 道题。')
    setCustom(JSON.stringify(records, null, 2)); setCount(records.length)
  }
  function start() {
    if (!job) return
    setConfirming(true)
  }
  const active = job && ['running', 'stopping'].includes(job.state)
  const chosen = job?.results.find(item => `${item.method}:${item.repeat}:${item.qid}` === selected)
  const chosenCalls = job?.calls?.filter(call => call.task_key === selected) ?? []
  const pendingCalls = methods.reduce((total, method) => total + callsPerMethod[method], 0) * count

  return <section className="evaluation-panel gsm8k-workspace" aria-label="GSM8K 论文复现实验">
    <div className="evaluation-section-heading"><div><h2>GSM8K 论文复现</h2><p>使用已验证的执行核心，比较四种方法。正式测试须在条件冻结后运行。</p></div><Button onClick={() => void action(refreshList)} disabled={busy}>刷新</Button></div>
    {error && <Alert type="error" showIcon title={error} closable onClose={() => setError('')} />}
    <div className="gsm8k-grid">
      <div className="evaluation-fields">
        <h3>创建实验</h3>
        <label>实验名称<Input aria-label="GSM8K 实验名称" value={name} onChange={e => setName(e.target.value)} maxLength={100} /></label>
        <label>题目来源<Select aria-label="GSM8K 题目来源" value={selection} onChange={value => { setSelection(value); setCount(value === 'formal' ? 100 : 1); if (value === 'formal') { setFake(false); setMethods(['single', 'reflection', 'majority', 'debate']) } }} options={[
          { value: 'debug', label: '固定训练调试集（10 题）' }, { value: 'calibration', label: '固定训练校准集（30 题）' }, { value: 'custom', label: '导入纯问题 JSON' }, { value: 'formal', label: '正式测试集（100 题，需先冻结条件）' },
        ]} /></label>
        {selection === 'formal' && <Alert type="info" showIcon title="正式测试需要先完成第六步并配置已冻结的条件与模型文件。" />}
        {selection === 'custom' && <><label>导入 JSON 文件<input type="file" aria-label="导入 GSM8K JSON 文件" accept=".json,application/json" onChange={e => { const file = e.target.files?.[0]; if (file) void action(() => importFile(file)); e.target.value = '' }} /></label>
          <label>题目 JSON<Input.TextArea aria-label="GSM8K 题目 JSON" rows={5} value={custom} onChange={e => setCustom(e.target.value)} placeholder={'[{"question":"问题","answer":"数字答案"}]'} /><small>每题提供问题和数字参考答案；参考答案只用于评分。</small></label></>}
        <label>题数<InputNumber aria-label="GSM8K 题数" min={1} max={selection === 'debug' ? 10 : selection === 'calibration' ? 30 : 100} disabled={selection === 'formal'} value={count} onChange={v => setCount(v ?? 1)} /></label>
        <fieldset><legend>复现方法</legend><Checkbox.Group disabled={selection === 'formal'} value={methods} onChange={v => setMethods(v as Method[])} options={Object.entries(labels).map(([value, label]) => ({ value, label }))} /></fieldset>
        <Checkbox checked={fake} disabled={selection === 'formal'} onChange={e => setFake(e.target.checked)}>使用固定假回答进行离线检查</Checkbox>
        <small>{count} 题 · {methods.length} 种方法 · 预计 {pendingCalls} 次正常生成，重试另计</small>
        <Button type="primary" disabled={busy || !name.trim() || !methods.length} onClick={() => void action(create)}>创建实验</Button>
      </div>
      <div className="evaluation-fields">
        <h3>实验记录</h3>
        <Select aria-label="选择 GSM8K 实验" placeholder="选择已有实验" value={job?.job_id} onChange={id => { setSelected(undefined); void action(() => refreshJob(id)) }} options={jobs.map(item => ({ value: item.job_id, label: `${item.name} · ${states[item.state] ?? item.state}` }))} />
        {job && <>
          <p><strong>{job.name}</strong> · {states[job.state] ?? job.state}{job.fake ? ' · 离线假回答' : ''}</p>
          <p>模型：{job.model}；{job.samples} 题；正常计划 {job.planned_calls} 次，已记录 {job.actual_calls} 次。</p>
          <p>完成 {job.counts.completed} · 失败 {job.counts.failed} · 中断 {job.counts.interrupted} · 待执行 {job.counts.pending}</p>
          {!job.fake && <small>本地预算上限 {job.budget_cny} 元；输入 {job.pricing.input_per_million_cny}、输出 {job.pricing.output_per_million_cny} 元／百万 Token。价格来源：{job.pricing.source}；核实时间：{job.pricing.verified_at}。请在启动前核对供应商价格。</small>}
          {job.error && <Alert type="warning" showIcon title={job.error} />}
          {job.report && <p>Token：输入 {job.report.usage.prompt_tokens ?? '未知'}，输出 {job.report.usage.completion_tokens ?? '未知'}；状态不明请求 {job.report.unknown_requests}。</p>}
          <Space wrap><Button type="primary" disabled={busy || !!active || job.state === 'completed' || !!job.counts.failed || !!job.counts.interrupted} onClick={start}>{job.state === 'draft' ? '启动实验' : '继续未执行题目'}</Button>
            <Button disabled={busy || job.state !== 'running'} onClick={() => void action(async () => { await evaluationRequest(`/gsm8k/jobs/${job.job_id}/stop`, 'POST'); await refreshJob(job.job_id) })}>当前题完成后暂停</Button>
            <Button disabled={busy} onClick={() => void action(() => refreshJob(job.job_id))}>刷新状态</Button></Space>
          {(!!job.counts.failed || !!job.counts.interrupted) && <Alert type="warning" showIcon title="存在失败或未确认任务，请先检查调用和费用；不会自动重跑。" />}
          <p>代码与题目指纹：{job.profile_sha256}</p>
        </>}
      </div>
    </div>
    {job && <>
      <h3>方法对比</h3>
      <div className="gsm8k-results">{job.methods.map(method => {
        const rows = job.results.filter(row => row.method === method)
        const correct = rows.filter(row => row.scores.fixed?.correct === 1).length
        const complete = rows.length === job.samples && rows.every(row => ['completed', 'failed', 'interrupted'].includes(row.status))
        const attempts = job.calls?.filter(call => call.task_key.startsWith(`${method}:`)).length ?? 0
        return <div className="gsm8k-summary" key={method}><strong>{labels[method]}</strong><span>完成 {rows.length}/{job.samples} 题 · 生成记录 {attempts} 次</span><span>固定评分：{complete ? `${correct}/${job.samples}（${(correct * 100 / job.samples).toFixed(1)}%）` : '未完成，暂不汇报完整集准确率'}</span></div>
      })}</div>
      <h3>逐题结果</h3>
      {job.results.length === 0 ? <p>尚无完成的结果。</p> : <div className="gsm8k-results">{job.results.map(row => {
        const key = `${row.method}:${row.repeat}:${row.qid}`
        const score = row.scores.fixed
        return <button type="button" key={key} aria-pressed={key === selected} onClick={() => setSelected(key)}>
          <strong>{row.qid} · {labels[row.method]}</strong><span>{row.status} · {score?.correct === 1 ? '正确' : score?.correct === 0 ? '未答对' : '未评分'} · 答案 {score?.prediction ?? '未解析'}</span>
        </button>
      })}</div>}
      {chosen && <div className="evaluation-fields"><h3>题目与轮次轨迹</h3><p>{chosen.question}</p><p>参考答案：{chosen.gold}</p><p>固定评分：{JSON.stringify(chosen.scores.fixed)}；作者评分：{JSON.stringify(chosen.scores.author_faithful)}</p>
        <Collapse items={chosenCalls.map((call, index) => ({ key: String(index), label: `第 ${call.round + 1} 轮 · 智能体 ${call.agent + 1} · ${call.status}${call.response?.finish_reason === 'length' ? ' · 输出截断' : ''}`, children: <>
          <p>结束原因：{call.response?.finish_reason ?? '未知'}；耗时：{call.duration_ms ?? '未知'} ms</p>
          <details><summary>本次消息</summary><pre>{JSON.stringify(call.request.messages, null, 2)}</pre></details>
          <details open><summary>模型回答</summary><pre>{call.response?.content ?? JSON.stringify(call.error)}</pre></details>
          <details><summary>用量</summary><pre>{JSON.stringify(call.response?.usage ?? null, null, 2)}</pre></details>
        </> }))} />
      </div>}
    </>}
    <Modal open={confirming} title={job?.fake ? '启动离线模拟？' : '确认调用真实模型？'} okText="确认启动" cancelText="取消" confirmLoading={busy}
      onCancel={() => setConfirming(false)} onOk={() => { if (!job) return; setConfirming(false); void action(async () => {
        await evaluationRequest(`/gsm8k/jobs/${job.job_id}/start`, 'POST', { allow_model_calls: !job.fake, retry_failed: false })
        await refreshJob(job.job_id); await refreshList()
      }) }}>
      <p>{job?.fake ? '本次使用固定假回答，不调用模型。' : `${job?.selection === 'formal' ? '本次为正式测试，题目及结果不能用于调试。' : ''}本次计划 ${job?.planned_calls ?? 0} 次正常模型生成，重试另计，实际费用以供应商账单为准。请确认已核实当前价格和预算。`}</p>
    </Modal>
  </section>
}
