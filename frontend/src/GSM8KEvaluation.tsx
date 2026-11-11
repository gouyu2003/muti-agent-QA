import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Checkbox, Collapse, Input, InputNumber, Modal, Select, Tag } from 'antd'
import { evaluationRequest } from './evaluationApi'
import { ReloadOutlined } from './Icons'

type Method = 'single' | 'reflection' | 'majority' | 'debate'
type Score = { correct: number | null; prediction?: string | null; parse_failure?: boolean }
type Result = { qid: string; method: Method; repeat: number; status: string; question: string; gold: string;
  outputs: string[]; scores: Record<string, Score>; error?: string; duration_ms?: number }
type Call = { task_key: string; agent: number; round: number; attempt: number; status: string; duration_ms: number | null;
  request: { messages?: { role: string; content: string }[] }; response: { content?: string; finish_reason?: string; usage?: unknown } | null; error: unknown }
type Job = { job_id: string; name: string; state: string; error: string | null; fake: boolean; model: string;
  methods: Method[]; selection: string; samples: number; planned_calls: number; actual_calls: number;
  counts: Record<string, number>; results: Result[]; calls?: Call[]; profile_sha256: string; budget_cny: string;
  frontend_observations?: { event: string; sha256: string | null; observed_at: string }[];
  pricing: { input_per_million_cny: string; output_per_million_cny: string; verified_at: string; source: string };
  report: { usage: Record<string, number | null>; unknown_requests: number } | null }

const labels: Record<Method, string> = { single: 'Single', reflection: 'Reflection', majority: 'Majority', debate: 'Debate' }
const callsPerMethod: Record<Method, number> = { single: 1, reflection: 2, majority: 6, debate: 6 }
const states: Record<string, string> = { draft: '待启动', running: '运行中', stopping: '当前题完成后暂停', paused: '已暂停', completed: '已完成', interrupted: '已中断' }
const stateColors: Record<string, string> = { draft: 'default', running: 'processing', stopping: 'warning', paused: 'default', completed: 'success', interrupted: 'error' }
const selectionLabels: Record<string, string> = { debug: '固定训练调试集', calibration: '固定训练校准集', custom: '导入题目', formal: '正式测试集' }

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

  return <section className="gsm8k-workspace" aria-label="GSM8K 论文复现实验">
    <header className="gsm8k-page-head">
      <div><h2>GSM8K 论文复现</h2><p>使用已验证的执行核心，比较四种方法。正式测试须在条件冻结后运行。</p></div>
      <Button icon={<ReloadOutlined />} onClick={() => void action(refreshList)} disabled={busy}>刷新列表</Button>
    </header>
    {error && <Alert type="error" showIcon title={error} closable onClose={() => setError('')} />}

    <section className="gsm8k-card" aria-labelledby="gsm8k-create-title">
      <div className="gsm8k-card-head">
        <h3 className="gsm8k-card-title" id="gsm8k-create-title"><span className="gsm8k-step">1</span>配置新实验<span className="gsm8k-card-note">先定题目范围与方法，再创建</span></h3>
      </div>
      <div className="gsm8k-card-body">
        <div className="gsm8k-fields">
          <label>实验名称<Input aria-label="GSM8K 实验名称" value={name} onChange={e => setName(e.target.value)} maxLength={100} /></label>
          <label>题目来源<Select aria-label="GSM8K 题目来源" value={selection} onChange={value => { setSelection(value); setCount(value === 'formal' ? 100 : 1); if (value === 'formal') { setFake(false); setMethods(['single', 'reflection', 'majority', 'debate']) } }} options={[
            { value: 'debug', label: '固定训练调试集（10 题）' }, { value: 'calibration', label: '固定训练校准集（30 题）' }, { value: 'custom', label: '导入纯问题 JSON' }, { value: 'formal', label: '正式测试集（100 题，需先冻结条件）' },
          ]} /></label>
          <label>题数<InputNumber aria-label="GSM8K 题数" min={1} max={selection === 'debug' ? 10 : selection === 'calibration' ? 30 : 100} disabled={selection === 'formal'} value={count} onChange={v => setCount(v ?? 1)} /></label>
          <fieldset><legend>复现方法</legend><Checkbox.Group disabled={selection === 'formal'} value={methods} onChange={v => setMethods(v as Method[])} options={Object.entries(labels).map(([value, label]) => ({ value, label }))} /></fieldset>
          {selection === 'formal' && <div className="gsm8k-fields-wide"><Alert type="info" showIcon title="正式测试需要先完成第六步并配置已冻结的条件与模型文件。" /></div>}
          {selection === 'custom' && <>
            <label className="gsm8k-fields-wide">导入 JSON 文件<input type="file" aria-label="导入 GSM8K JSON 文件" accept=".json,application/json" onChange={e => { const file = e.target.files?.[0]; if (file) void action(() => importFile(file)); e.target.value = '' }} /></label>
            <label className="gsm8k-fields-wide">题目 JSON<Input.TextArea aria-label="GSM8K 题目 JSON" rows={5} value={custom} onChange={e => setCustom(e.target.value)} placeholder={'[{"question":"问题","answer":"数字答案"}]'} /><small>每题提供问题和数字参考答案；参考答案只用于评分。</small></label>
          </>}
          <div className="gsm8k-fields-wide gsm8k-check"><Checkbox checked={fake} disabled={selection === 'formal'} onChange={e => setFake(e.target.checked)}>使用固定假回答进行离线检查</Checkbox></div>
        </div>
      </div>
      <div className="gsm8k-card-foot">
        <small>{selectionLabels[selection]} · {count} 题 · {methods.length} 种方法 · 预计 {pendingCalls} 次正常生成，重试另计</small>
        <Button type="primary" disabled={busy || !name.trim() || !methods.length} onClick={() => void action(create)}>创建实验</Button>
      </div>
    </section>

    <section className="gsm8k-card" aria-labelledby="gsm8k-record-title">
      <div className="gsm8k-card-head">
        <h3 className="gsm8k-card-title" id="gsm8k-record-title"><span className="gsm8k-step">2</span>实验记录</h3>
        <div className="gsm8k-card-actions">
          {job && <Tag color={stateColors[job.state]}>{states[job.state] ?? job.state}</Tag>}
          {job && <Button type="primary" disabled={busy || !!active || job.state === 'completed' || !!job.counts.failed || !!job.counts.interrupted} onClick={start}>{job.state === 'draft' ? '启动实验' : '继续未执行题目'}</Button>}
          {job && <Button disabled={busy || job.state !== 'running'} onClick={() => void action(async () => { await evaluationRequest(`/gsm8k/jobs/${job.job_id}/stop`, 'POST'); await refreshJob(job.job_id) })}>当前题完成后暂停</Button>}
          <Button type="text" icon={<ReloadOutlined />} aria-label="刷新状态" title="刷新状态" disabled={busy || !job} onClick={() => { if (job) void action(() => refreshJob(job.job_id)) }} />
        </div>
      </div>
      <div className="gsm8k-card-body">
        <div className="gsm8k-field gsm8k-job-select"><span>实验</span>
          <Select aria-label="选择 GSM8K 实验" placeholder="选择已有实验" value={job?.job_id} onChange={id => { setSelected(undefined); void action(() => refreshJob(id)) }} options={jobs.map(item => ({ value: item.job_id, label: `${item.name} · ${states[item.state] ?? item.state}` }))} />
        </div>
        {!job && <p className="gsm8k-empty">尚无实验。先在「配置新实验」中创建，或从上方选择一个已有实验。</p>}
        {job && <>
          <div className="gsm8k-stats">
            <div className="gsm8k-stat"><strong>{job.counts.completed}/{job.samples * job.methods.length}</strong><span>完成任务</span></div>
            <div className="gsm8k-stat" data-tone={job.counts.failed ? 'warn' : undefined}><strong>{job.counts.failed}</strong><span>失败</span></div>
            <div className="gsm8k-stat" data-tone={job.counts.interrupted ? 'warn' : undefined}><strong>{job.counts.interrupted}</strong><span>中断</span></div>
            <div className="gsm8k-stat"><strong>{job.counts.pending}</strong><span>待执行</span></div>
            <div className="gsm8k-stat"><strong>{job.actual_calls}/{job.planned_calls}</strong><span>已记录 / 计划调用</span></div>
          </div>
          <p className="gsm8k-meta"><span>模型 {job.model}</span><span>{job.samples} 题</span>{job.fake && <span>离线假回答</span>}</p>
          {job.error && <Alert type="warning" showIcon title={job.error} />}
          {(!!job.counts.failed || !!job.counts.interrupted) && <Alert type="warning" showIcon title="存在失败或未确认任务，请先检查调用和费用；不会自动重跑。" />}
          <div className="gsm8k-folds">
            <details className="gsm8k-fold">
              <summary>模型与费用</summary>
              {job.fake ? <p className="gsm8k-hint">本次使用固定假回答，不调用模型，不产生费用。</p> : <>
                <dl className="gsm8k-kv">
                  <div><dt>本地预算上限</dt><dd>{job.budget_cny} 元</dd></div>
                  <div><dt>输入单价</dt><dd>{job.pricing.input_per_million_cny} 元／百万 Token</dd></div>
                  <div><dt>输出单价</dt><dd>{job.pricing.output_per_million_cny} 元／百万 Token</dd></div>
                  <div><dt>价格来源</dt><dd>{job.pricing.source || '未记录'}</dd></div>
                  <div><dt>核实时间</dt><dd>{job.pricing.verified_at || '未记录'}</dd></div>
                </dl>
                <p className="gsm8k-hint">启动前请核对供应商价格，实际费用以供应商账单为准。</p>
              </>}
              {job.report && <p className="gsm8k-hint">Token：输入 {job.report.usage.prompt_tokens ?? '未知'}，输出 {job.report.usage.completion_tokens ?? '未知'}；状态不明请求 {job.report.unknown_requests}。</p>}
            </details>
            <details className="gsm8k-fold">
              <summary>审计信息</summary>
              <dl className="gsm8k-kv">
                <div><dt>代码与题目指纹</dt><dd className="gsm8k-fingerprint">{job.profile_sha256}</dd></div>
                <div><dt>实验标识</dt><dd>{job.job_id}</dd></div>
                <div><dt>题目来源</dt><dd>{selectionLabels[job.selection] ?? job.selection}</dd></div>
                {job.frontend_observations?.map((item, index) => <div key={`${item.event}-${item.observed_at}-${index}`}>
                  <dt>{item.event === 'created' ? '创建时前端' : item.event === 'started' ? '启动时前端' : '前端记录'}</dt>
                  <dd className="gsm8k-fingerprint">{item.sha256 ?? '未能读取'} · {item.observed_at}</dd>
                </div>)}
              </dl>
              <p className="gsm8k-hint">前端指纹记录的是后端本机源码，不代表浏览器实际加载的资源版本。</p>
            </details>
          </div>
        </>}
      </div>
    </section>

    {job && <section className="gsm8k-card" aria-labelledby="gsm8k-compare-title">
      <div className="gsm8k-card-head">
        <h3 className="gsm8k-card-title" id="gsm8k-compare-title"><span className="gsm8k-step">3</span>方法对比</h3>
        <span className="gsm8k-card-note">{job.methods.length} 种方法 · 每方法 {job.samples} 题</span>
      </div>
      <div className="gsm8k-card-body">
        <div className="gsm8k-methods">{job.methods.map(method => {
          const rows = job.results.filter(row => row.method === method)
          const correct = rows.filter(row => row.scores.fixed?.correct === 1).length
          const complete = rows.length === job.samples && rows.every(row => ['completed', 'failed', 'interrupted'].includes(row.status))
          const attempts = job.calls?.filter(call => call.task_key.startsWith(`${method}:`)).length ?? 0
          return <div className="gsm8k-method" key={method}>
            <div className="gsm8k-method-head"><strong>{labels[method]}</strong><span>完成 {rows.length}/{job.samples} 题</span></div>
            <div className="gsm8k-method-score">{complete ? `${(correct * 100 / job.samples).toFixed(1)}%` : '—'}<small>{complete ? `${correct}/${job.samples} 题` : '待整集完成'}</small></div>
            <span className="gsm8k-method-meta">生成记录 {attempts} 次</span>
            <span className="gsm8k-method-meta">固定评分：{complete ? `${correct}/${job.samples}` : '待整集完成'}</span>
          </div>
        })}</div>
      </div>
      <div className="gsm8k-card-foot">
        <small>固定评分使用固定答案序列，需整集 {job.samples} 题全部结束才汇报准确率；重试与中断题按原状态保留。</small>
      </div>
    </section>}

    {job && <section className="gsm8k-card" aria-labelledby="gsm8k-results-title">
      <div className="gsm8k-card-head">
        <h3 className="gsm8k-card-title" id="gsm8k-results-title"><span className="gsm8k-step">4</span>逐题结果</h3>
        {job.results.length > 0 && <span className="gsm8k-card-note">已完成 {job.results.length} 条记录</span>}
      </div>
      <div className="gsm8k-card-body">
        {job.results.length === 0
          ? <p className="gsm8k-empty">启动实验后，逐题结果与轮次轨迹将显示在这里。</p>
          : <div className="gsm8k-results">{job.results.map(row => {
            const key = `${row.method}:${row.repeat}:${row.qid}`
            const score = row.scores.fixed
            return <button type="button" key={key} aria-pressed={key === selected} onClick={() => setSelected(key)}>
              <strong>{row.qid} · {labels[row.method]}</strong><span>{row.status} · {score?.correct === 1 ? '正确' : score?.correct === 0 ? '未答对' : '未评分'} · 答案 {score?.prediction ?? '未解析'}</span>
            </button>
          })}</div>}
        {chosen && <div className="gsm8k-detail">
          <h4>题目与轮次轨迹</h4>
          <div className="gsm8k-detail-grid">
            <div><span>题目</span><p>{chosen.question}</p></div>
            <div><span>参考答案</span><p>{chosen.gold}</p></div>
          </div>
          <p className="gsm8k-hint">固定评分：{JSON.stringify(chosen.scores.fixed)}；作者评分：{JSON.stringify(chosen.scores.author_faithful)}</p>
          <Collapse items={chosenCalls.map((call, index) => ({ key: String(index), label: `第 ${call.round + 1} 轮 · 智能体 ${call.agent + 1} · ${call.status}${call.response?.finish_reason === 'length' ? ' · 输出截断' : ''}`, children: <>
            <p>结束原因：{call.response?.finish_reason ?? '未知'}；耗时：{call.duration_ms ?? '未知'} ms</p>
            <details><summary>本次消息</summary><pre>{JSON.stringify(call.request.messages, null, 2)}</pre></details>
            <details open><summary>模型回答</summary><pre>{call.response?.content ?? JSON.stringify(call.error)}</pre></details>
            <details><summary>用量</summary><pre>{JSON.stringify(call.response?.usage ?? null, null, 2)}</pre></details>
          </> }))} />
        </div>}
      </div>
    </section>}

    <Modal open={confirming} title={job?.fake ? '启动离线模拟？' : '确认调用真实模型？'} okText="确认启动" cancelText="取消" confirmLoading={busy}
      onCancel={() => setConfirming(false)} onOk={() => { if (!job) return; setConfirming(false); void action(async () => {
        await evaluationRequest(`/gsm8k/jobs/${job.job_id}/start`, 'POST', { allow_model_calls: !job.fake, retry_failed: false })
        await refreshJob(job.job_id); await refreshList()
      }) }}>
      <p>{job?.fake ? '本次使用固定假回答，不调用模型。' : `${job?.selection === 'formal' ? '本次为正式测试，题目及结果不能用于调试。' : ''}本次计划 ${job?.planned_calls ?? 0} 次正常模型生成，重试另计，实际费用以供应商账单为准。请确认已核实当前价格和预算。`}</p>
    </Modal>
  </section>
}
