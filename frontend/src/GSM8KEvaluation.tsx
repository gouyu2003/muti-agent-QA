import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Checkbox, Collapse, Drawer, Empty, Input, InputNumber, Modal, Pagination, Select, Tag } from 'antd'
import { Plus } from 'lucide-react'
import { evaluationRequest } from './evaluationApi'
import { ReloadOutlined, SearchOutlined } from './Icons'
import EvaluationJobActions from './EvaluationJobActions'

type Method = 'single' | 'reflection' | 'majority' | 'debate'
type Score = { correct: number | null; prediction?: string | null; parse_failure?: boolean;
  candidates?: (string | null)[]; votes?: Record<string, number>; tie?: boolean; truncated?: number }
type Result = { qid: string; method: Method; repeat: number; status: string; question: string; gold: string;
  outputs: string[]; scores: Record<string, Score>; error?: string; duration_ms?: number | null;
  category?: string; round_scores?: Score[] }
type Call = { task_key: string; agent: number; round: number; attempt: number; status: string; duration_ms: number | null;
  request: { messages?: { role: string; content: string }[] }; response: { content?: string; finish_reason?: string; usage?: unknown } | null; error: unknown }
type Job = { job_id: string; name: string; state: string; error: string | null; fake: boolean; model: string;
  archived?: boolean;
  methods: Method[]; selection: string; samples: number; planned_calls: number; actual_calls: number; next_stage_calls?: number;
  pending_calls?: number; can_resume_pending?: boolean; pending_resume_count?: number; superseded_calls?: number;
  counts: Record<string, number>; results: Result[]; calls?: Call[]; profile_sha256: string; budget_cny: string;
  frontend_observations?: { event: string; sha256: string | null; observed_at: string }[];
  pricing?: { input_per_million_cny: string; output_per_million_cny: string; verified_at: string; source: string };
  dataset_revision?: string; manifest_sha256?: string; scoring_version?: string; seed?: number; quotas?: Record<string, number>;
  method_metrics?: Partial<Record<Method, { calls: number; usage: Record<string, number | null> }>>;
  report: { usage: Record<string, number | null>; unknown_requests: number } | null }
type Manifest = { selection: string; count?: number; split?: string; revision?: string;
  quotas?: Record<string, number>; seed?: number; error?: string }
type Benchmark = 'gsm8k' | 'mmlu-pro'

const labels: Record<Method, string> = { single: 'Single', reflection: 'Reflection', majority: 'Majority', debate: 'Debate' }
const allMethods = Object.keys(labels) as Method[]
const callsPerMethod: Record<Method, number> = { single: 1, reflection: 2, majority: 6, debate: 6 }
const states: Record<string, string> = { draft: '待启动', running: '运行中', stopping: '当前题完成后暂停', paused: '已暂停', completed: '已完成', interrupted: '已中断' }
const stateColors: Record<string, string> = { draft: 'default', running: 'processing', stopping: 'warning', paused: 'default', completed: 'success', interrupted: 'error' }
const selectionLabels: Record<string, string> = { debug: '固定训练调试集', calibration: '固定训练校准集',
  custom: '导入题目', formal: '正式测试集', smoke: '5 题调试', development: '100 题开发',
  'calibration-60': '60 题难度校准', 'comparison-100': '100 题四组对照' }
const subjectLabels: Record<string, string> = { math: '数学', physics: '物理', engineering: '工程', business: '商业',
  chemistry: '化学', economics: '经济学', law: '法律', philosophy: '哲学' }

type DetailView = 'overview' | 'compare' | 'results'
const viewLabels: Record<DetailView, string> = { overview: '概览', compare: '方法对比', results: '逐题结果' }
function readUrlState(benchmark: Benchmark): { jobId: string | null; view: DetailView } {
  const params = new URLSearchParams(window.location.search)
  const view = params.get(`${benchmark}_view`)
  return { jobId: params.get(`${benchmark}_job`), view: view === 'compare' || view === 'results' ? view : 'overview' }
}
function writeUrlState(benchmark: Benchmark, jobId: string | undefined, view: DetailView, push = false) {
  const params = new URLSearchParams(window.location.search)
  if (jobId) params.set(`${benchmark}_job`, jobId)
  else params.delete(`${benchmark}_job`)
  if (view === 'overview') params.delete(`${benchmark}_view`)
  else params.set(`${benchmark}_view`, view)
  const query = params.toString()
  const url = `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`
  if (push) window.history.pushState(null, '', url)
  else window.history.replaceState(null, '', url)
}

type QuestionRow = Pick<Result, 'qid' | 'repeat' | 'gold' | 'category'> & { results: Partial<Record<Method, Result>> }
const resultFilters = { all: '全部题目', incorrect: '有方法答错', disagreement: '方法间答案不同' } as const
const resultStates: Record<string, string> = { pending: '待执行', running: '运行中', failed: '执行失败', interrupted: '已中断' }
const resultsPageSize = 20

function QuestionResults({ job, multipleChoice }: { job: Job; multipleChoice: boolean }) {
  const [filter, setFilter] = useState<keyof typeof resultFilters>('all')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<string>()
  const [trace, setTrace] = useState<{ key: string; calls?: Call[]; error?: string }>()
  const chosen = job.results.find(result => `${result.method}:${result.repeat}:${result.qid}` === selected)
  const tracePath = multipleChoice && chosen ? `/mmlu-pro/jobs/${job.job_id}/questions/${chosen.qid}/${chosen.method}` : null
  const activeTrace = trace?.key === tracePath ? trace : undefined
  const chosenCalls = multipleChoice ? activeTrace?.calls ?? [] : job.calls?.filter(call => call.task_key === selected) ?? []
  useEffect(() => {
    if (!tracePath) return
    let current = true
    void evaluationRequest<{ items: Call[] }>(tracePath).then(data => {
      if (current) setTrace({ key: tracePath, calls: data.items })
    }).catch(e => {
      if (current) setTrace({ key: tracePath, error: e instanceof Error ? e.message : '无法读取轨迹' })
    })
    return () => { current = false }
  }, [tracePath, job.actual_calls, chosen?.status])

  const questions = new Map<string, QuestionRow>()
  for (const result of job.results) {
    const { qid, repeat, gold, method } = result
    const key = `${repeat}:${qid}`
    const row = questions.get(key) ?? { qid, repeat, gold, category: result.category, results: {} }
    row.results[method] = result
    questions.set(key, row)
  }
  const rows = [...questions.values()]
  const showRepeat = rows.some(row => row.repeat > 0)
  const filtered = rows.filter(row => {
    if (filter === 'all') return true
    const scores = Object.values(row.results).filter(result => result.status === 'completed').map(result => result.scores.fixed)
    if (filter === 'incorrect') return scores.some(score => score?.correct === 0)
    const predictions = scores.map(score => score?.prediction).filter(prediction => prediction != null)
    return new Set(predictions).size > 1
  })
  const currentPage = Math.min(page, Math.max(1, Math.ceil(filtered.length / resultsPageSize)))
  const visibleRows = filtered.slice((currentPage - 1) * resultsPageSize, currentPage * resultsPageSize)

  return <div className="gsm8k-card-body">
    {rows.length === 0 ? <p className="gsm8k-empty">启动实验后，逐题结果与轮次轨迹将显示在这里。</p> : <>
      <p className="gsm8k-card-note">{rows.length} {showRepeat ? '组题目结果' : '题'} · {job.results.length} 条方法记录</p>
      <div className="gsm8k-results-toolbar">
        <div className="gsm8k-results-filters" role="group" aria-label="筛选逐题结果">
          {(Object.keys(resultFilters) as (keyof typeof resultFilters)[]).map(value =>
            <Button key={value} size="small" type={filter === value ? 'primary' : 'default'} aria-pressed={filter === value}
              onClick={() => { setFilter(value); setPage(1) }}>{resultFilters[value]}</Button>)}
        </div>
        <span className="gsm8k-card-note" role="status">{filter !== 'all' && `符合筛选 ${filtered.length} ${showRepeat ? '组' : '题'}`}</span>
      </div>
      {visibleRows.length === 0 ? <p className="gsm8k-empty">没有符合当前筛选的题目。</p> : <>
        <div className="gsm8k-results-scroll" role="region" aria-label="逐题对照表滚动区域" tabIndex={0}>
          <table className="gsm8k-results-table" aria-label="逐题结果"
            style={{ minWidth: `${job.methods.length * 8 + (showRepeat ? 20 : 16)}rem` }}>
            <thead><tr>
              <th scope="col" className="gsm8k-results-qid">题号</th>
              {showRepeat && <th scope="col" className="gsm8k-results-repeat">重复</th>}
              {multipleChoice && <th scope="col">学科</th>}
              <th scope="col" className="gsm8k-results-gold">标准答案</th>
              {job.methods.map(method => <th scope="col" key={method}>{labels[method]}</th>)}
            </tr></thead>
            <tbody>{visibleRows.map(row => {
              const gold = row.gold.split('####').at(-1)?.trim() || '—'
              return <tr key={`${row.repeat}:${row.qid}`}>
                <th scope="row">{row.qid}</th>
                {showRepeat && <td>{row.repeat + 1}</td>}
                {multipleChoice && <td>{subjectLabels[row.category ?? ''] ?? row.category}</td>}
                <td><span className="gsm8k-result-number" title={gold}>{gold}</span></td>
                {job.methods.map(method => {
                  const result = row.results[method]
                  if (!result) return <td key={method}><span className="gsm8k-result-state">待出结果</span></td>
                  const score = result.scores.fixed
                  const completed = result.status === 'completed'
                  const answer = completed ? score?.prediction ?? '未解析' : undefined
                  const status = completed
                    ? score?.correct === 1 ? '正确' : score?.correct === 0 ? '未答对' : '未评分'
                    : resultStates[result.status] ?? result.status
                  const needsReview = completed ? score?.correct === 0 : ['failed', 'interrupted'].includes(result.status)
                  return <td key={method} className="gsm8k-result-cell">
                    <button type="button" aria-haspopup="dialog" data-review={needsReview || undefined}
                      aria-label={[row.qid, showRepeat ? `第 ${row.repeat + 1} 次` : null, labels[method], answer, status, '查看详情'].filter(Boolean).join(' · ')}
                      onClick={() => setSelected(`${method}:${row.repeat}:${row.qid}`)}>
                      {answer && <span className="gsm8k-result-number" title={answer}>{answer}</span>}
                      <span className="gsm8k-result-state">{status}</span>
                    </button>
                  </td>
                })}
              </tr>
            })}</tbody>
          </table>
        </div>
        {filtered.length > resultsPageSize && <Pagination className="gsm8k-results-pagination" size="small" current={currentPage}
          total={filtered.length} pageSize={resultsPageSize} showSizeChanger={false} onChange={setPage} />}
      </>}
    </>}
    <Modal open={!!chosen} title={chosen && `题目与轮次轨迹 · ${chosen.qid} · ${labels[chosen.method]}${showRepeat ? ` · 第 ${chosen.repeat + 1} 次` : ''}`}
      footer={null} width={880} onCancel={() => setSelected(undefined)}>
      {chosen && <div className="gsm8k-detail">
        <div className={multipleChoice ? 'gsm8k-detail-grid gsm8k-choice-detail' : 'gsm8k-detail-grid'}>
          <div><span>题目</span>{multipleChoice ? <pre>{chosen.question}</pre> : <p>{chosen.question}</p>}</div>
          <div><span>参考答案</span><p>{chosen.gold}</p></div>
        </div>
        {chosen.error && <p className="gsm8k-hint">执行错误：{chosen.error}</p>}
        {multipleChoice ? chosen.round_scores?.map((score, round) =>
          <p className="gsm8k-hint" key={round}>第 {round + 1} 轮投票 · {(score.candidates ?? []).map(choice => choice ?? '无效').join(' / ')}
            {' · '}{Object.entries(score.votes ?? {}).map(([choice, votes]) => `${choice} ${votes} 票`).join(' / ') || '无有效票'}
            {' · '}{score.tie ? '平票' : score.prediction ?? '无预测'}
            {score.prediction && (score.correct ? '（答对）' : '（答错）')}</p>)
          : <p className="gsm8k-hint">固定评分：{JSON.stringify(chosen.scores.fixed)}；作者评分：{JSON.stringify(chosen.scores.author_faithful)}</p>}
        {activeTrace?.error && <Alert type="error" showIcon title={activeTrace.error} />}
        {chosenCalls.length > 0 ? <Collapse items={chosenCalls.map((call, index) => ({ key: String(index), label: `${call.attempt > 1 ? `第 ${call.attempt} 次尝试 · ` : ''}第 ${call.round + 1} 轮 · 智能体 ${call.agent + 1} · ${call.status}${call.response?.finish_reason === 'length' ? ' · 输出截断' : ''}`, children: <>
          <p>结束原因：{call.response?.finish_reason ?? '未知'}；耗时：{call.duration_ms ?? '未知'} ms</p>
          <details><summary>本次消息</summary><pre>{JSON.stringify(call.request.messages, null, 2)}</pre></details>
          <details open><summary>模型回答</summary><pre>{call.response?.content ?? JSON.stringify(call.error)}</pre></details>
          <details><summary>用量</summary><pre>{JSON.stringify(call.response?.usage ?? null, null, 2)}</pre></details>
        </> }))} /> : <>
          <p className="gsm8k-hint">{tracePath && !activeTrace ? '正在读取轮次轨迹…' : '暂无轮次轨迹。'}</p>
          {chosen.outputs.length > 0 && <details><summary>已保存的模型输出</summary><pre>{chosen.outputs.join('\n\n')}</pre></details>}
        </>}
      </div>}
    </Modal>
  </div>
}

export default function GSM8KEvaluation({ benchmark = 'gsm8k' }: { benchmark?: Benchmark }) {
  const multipleChoice = benchmark === 'mmlu-pro'
  const title = multipleChoice ? 'MMLU-Pro 难度预实验' : 'GSM8K 论文复现'
  const benchmarkName = multipleChoice ? 'MMLU-Pro' : 'GSM8K'
  const jobsPath = `/${benchmark}/jobs`
  const [manifests, setManifests] = useState<Manifest[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [job, setJob] = useState<Job>()
  const [view, setView] = useState<DetailView>(() => readUrlState(benchmark).view)
  const [createOpen, setCreateOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const [name, setName] = useState(multipleChoice ? 'MMLU-Pro 5 题调试' : 'GSM8K 训练题复现')
  const [selection, setSelection] = useState(multipleChoice ? 'smoke' : 'debug')
  const [count, setCount] = useState(1)
  const [methods, setMethods] = useState<Method[]>(allMethods)
  const [custom, setCustom] = useState('')
  const [fake, setFake] = useState(multipleChoice)
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  const jobRequest = useRef(0)

  const refreshList = useCallback(async () => {
    const data = await evaluationRequest<{ items: Job[] }>(showArchived ? `${jobsPath}?include_archived=true` : jobsPath)
    setJobs(data.items)
    return data.items
  }, [jobsPath, showArchived])
  const refreshJob = useCallback(async (id: string) => {
    const request = ++jobRequest.current
    const data = await evaluationRequest<Job>(`${jobsPath}/${id}`)
    if (request !== jobRequest.current) return
    setJob(data)
    setJobs(previous => previous.map(item => item.job_id === id ? data : item))
  }, [jobsPath])
  const refreshManifests = useCallback(async () => {
    if (multipleChoice) setManifests((await evaluationRequest<{ items: Manifest[] }>('/mmlu-pro/manifests')).items)
  }, [multipleChoice])
  useEffect(() => {
    void (async () => {
      try {
        const [items] = await Promise.all([refreshList(), refreshManifests()])
        const { jobId } = readUrlState(benchmark)
        const target = items.find(item => item.job_id === jobId) ?? items[0]
        if (target) {
          await refreshJob(target.job_id)
          writeUrlState(benchmark, target.job_id, readUrlState(benchmark).view)
        } else {
          jobRequest.current += 1
          setJob(undefined)
          writeUrlState(benchmark, undefined, 'overview')
        }
      } catch (e) { setError(e instanceof Error ? e.message : '读取失败') }
    })()
  }, [benchmark, refreshList, refreshJob, refreshManifests])
  useEffect(() => {
    if (busy || !job || !['running', 'stopping'].includes(job.state)) return
    const timer = window.setInterval(() => { void refreshJob(job.job_id).catch(e => setError(e instanceof Error ? e.message : '读取失败')) }, 2000)
    return () => window.clearInterval(timer)
  }, [busy, job?.job_id, job?.state, refreshJob])
  useEffect(() => {
    const onPopState = () => {
      const { jobId, view: urlView } = readUrlState(benchmark)
      setView(urlView)
      if (!jobId) { jobRequest.current += 1; setJob(undefined); return }
      void refreshJob(jobId).catch(e => setError(e instanceof Error ? e.message : '读取失败'))
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [benchmark, refreshJob])

  const visibleJobs = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    return keyword ? jobs.filter(item => `${item.name} ${item.job_id}`.toLowerCase().includes(keyword)) : jobs
  }, [jobs, search])

  async function action(fn: () => Promise<void>) {
    setBusy(true); setError('')
    try { await fn() } catch (e) { setError(e instanceof Error ? e.message : '操作失败') } finally { setBusy(false) }
  }
  async function selectJob(id: string) {
    await refreshJob(id)
    writeUrlState(benchmark, id, view, true)
  }
  async function organizeJob(item: Job, archived?: boolean) {
    return action(async () => {
      const path = `${jobsPath}/${item.job_id}`
      if (archived === undefined) await evaluationRequest(path, 'DELETE')
      else await evaluationRequest(`${path}/archive`, 'POST', { archived })
      const keepVisible = archived !== undefined && (!archived || showArchived)
      setJobs(previous => previous.flatMap(row => row.job_id === item.job_id
        ? keepVisible ? [{ ...row, archived }] : [] : [row]))
      if (item.job_id !== job?.job_id) return
      jobRequest.current += 1
      if (keepVisible) {
        setJob({ ...job, archived })
        return
      }
      const next = jobs.find(row => row.job_id !== item.job_id)
      setJob(undefined)
      setView('overview')
      writeUrlState(benchmark, next?.job_id, 'overview')
      if (next) await refreshJob(next.job_id)
    })
  }
  function changeView(next: DetailView) {
    setView(next)
    writeUrlState(benchmark, job?.job_id, next)
  }
  async function refreshAll() {
    await Promise.all([refreshList(), refreshManifests()])
    if (job) await refreshJob(job.job_id)
  }
  async function create() {
    let records: unknown = null
    if (selection === 'custom') {
      try { records = JSON.parse(custom) } catch { throw new Error('题目须为 JSON 数组，每项包含 question 和 answer。') }
      if (!Array.isArray(records) || records.length !== count) throw new Error('自定义题目数量需与题数一致。')
    }
    const data = await evaluationRequest<Job>(jobsPath, 'POST',
      multipleChoice ? { name, methods, selection, fake } : { name, methods, selection, count, records, fake })
    await refreshList(); await refreshJob(data.job_id)
    setView('overview'); setCreateOpen(false)
    writeUrlState(benchmark, data.job_id, 'overview', true)
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
  const manifest = manifests.find(item => item.selection === selection)
  const sampleCount = multipleChoice ? manifest?.count : count
  const pendingCalls = methods.reduce((total, method) => total + callsPerMethod[method], 0) * (sampleCount ?? 0)
  const remainingCalls = job?.pending_calls ?? (job?.planned_calls ?? 0) - (job?.actual_calls ?? 0)
  const nextStageCalls = job?.next_stage_calls ?? remainingCalls
  const resuming = multipleChoice && !!job?.can_resume_pending

  return <section className="gsm8k-workspace" aria-label={multipleChoice ? title : 'GSM8K 论文复现实验'}>
    <header className="gsm8k-page-head">
      <div><h2>{title}</h2><p>{multipleChoice ? '在固定题单上比较四种方法，查看每轮选项与投票。' : '使用已验证的执行核心，比较四种方法。正式测试须在条件冻结后运行。'}</p></div>
      <div className="gsm8k-page-actions">
        <Button icon={<ReloadOutlined />} onClick={() => void action(refreshAll)} disabled={busy}>刷新</Button>
        <Button type="primary" icon={<Plus size={14} aria-hidden="true" />} onClick={() => { setError(''); setCreateOpen(true) }}>新建实验</Button>
      </div>
    </header>
    {!createOpen && error && <Alert type="error" showIcon title={error} closable onClose={() => setError('')} />}

    <div className="gsm8k-layout">
      <aside className="gsm8k-rail" aria-label="实验记录">
        <Input aria-label="搜索实验记录" prefix={<SearchOutlined />} placeholder="搜索名称或 ID" value={search} onChange={event => setSearch(event.target.value)} allowClear />
        <Checkbox checked={showArchived} disabled={busy} onChange={event => setShowArchived(event.target.checked)}>显示已归档实验</Checkbox>
        {!jobs.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无实验记录" /> :
          !visibleJobs.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的实验" /> :
            <div className="gsm8k-job-list">{visibleJobs.map(item =>
              <div className="evaluation-job-card" key={item.job_id}>
                <button type="button" disabled={busy} aria-pressed={item.job_id === job?.job_id}
                  className={`evaluation-card-select${item.job_id === job?.job_id ? ' selected' : ''}`} onClick={() => void action(() => selectJob(item.job_id))}>
                  <strong>{item.name}</strong>
                  <span className="gsm8k-list-state">
                    <Tag color={stateColors[item.state]}>{states[item.state] ?? item.state}</Tag>
                    <span>{item.counts?.completed ?? 0}/{item.samples * (item.methods?.length ?? 0)}</span>
                  </span>
                  <span>{item.methods?.length ?? 0} 种方法 · {item.samples} 题{item.fake ? ' · 离线模拟' : ''}{item.archived ? ' · 已归档' : ''}</span>
                </button>
                <EvaluationJobActions name={item.name} archived={item.archived} busy={busy}
                  active={['running', 'stopping'].includes(item.state)}
                  onArchive={() => void organizeJob(item, !item.archived)} onDelete={() => organizeJob(item)} />
              </div>)}</div>}
      </aside>

      {job ? <section className="gsm8k-detail-panel" aria-label="实验详情">
        <div className="gsm8k-detail-header">
          <div className="gsm8k-detail-title">
            <h2>{job.name}</h2>
            <Tag color={stateColors[job.state]}>{states[job.state] ?? job.state}</Tag>
            {job.archived && <Tag>已归档</Tag>}
          </div>
          <div className="gsm8k-detail-actions">
            <Button type="primary" disabled={busy || !!active || job.archived || job.state === 'completed' ||
              (!resuming && ((multipleChoice && job.state === 'interrupted') || !!job.counts.failed || !!job.counts.interrupted))}
              onClick={start}>{resuming ? '仅续跑待执行题目' : job.state === 'draft' ? '启动实验' : '继续未执行题目'}</Button>
            <Button disabled={busy || job.state !== 'running'} onClick={() => void action(async () => { await evaluationRequest(`${jobsPath}/${job.job_id}/stop`, 'POST'); await refreshJob(job.job_id) })}>当前题完成后暂停</Button>
          </div>
          <div className="gsm8k-detail-views" role="group" aria-label="实验详情视图">
            {(Object.keys(viewLabels) as DetailView[]).map(key =>
              <Button key={key} type="text" aria-pressed={view === key} className={view === key ? 'active' : ''} onClick={() => changeView(key)}>{viewLabels[key]}</Button>)}
          </div>
        </div>
        <div className="gsm8k-view-body">
          {view === 'overview' && <div className="gsm8k-card-body">
            <div className="gsm8k-stats">
              <div className="gsm8k-stat"><strong>{job.counts.completed}/{job.samples * job.methods.length}</strong><span>完成任务</span></div>
              <div className="gsm8k-stat" data-tone={job.counts.failed ? 'warn' : undefined}><strong>{job.counts.failed}</strong><span>失败</span></div>
              <div className="gsm8k-stat" data-tone={job.counts.interrupted ? 'warn' : undefined}><strong>{job.counts.interrupted}</strong><span>中断</span></div>
              <div className="gsm8k-stat"><strong>{job.counts.pending}</strong><span>待执行</span></div>
              <div className="gsm8k-stat"><strong>{job.actual_calls}/{job.planned_calls}</strong><span>已记录 / 计划调用</span></div>
            </div>
            <p className="gsm8k-meta"><span>模型 {job.model}</span><span>{job.samples} 题</span>{job.fake && <span>离线假回答</span>}</p>
            {!!job.superseded_calls && <p className="gsm8k-hint">已记录调用包含 {job.superseded_calls} 次补跑前的历史调用。</p>}
            {job.error && <Alert type={job.state === 'paused' ? 'info' : 'warning'} showIcon title={job.error} />}
            {(!!job.counts.failed || !!job.counts.interrupted) && <Alert type="warning" showIcon title="存在失败或未确认任务，请先检查调用和费用；不会自动重跑。" />}
            <div className="gsm8k-folds">
              <details className="gsm8k-fold">
                <summary>模型与费用</summary>
                {job.fake ? <p className="gsm8k-hint">本次使用固定假回答，不调用模型，不产生费用。</p> : <>
                  {job.pricing && <dl className="gsm8k-kv">
                    <div><dt>本地预算上限</dt><dd>{job.budget_cny} 元</dd></div>
                    <div><dt>输入单价</dt><dd>{job.pricing.input_per_million_cny} 元／百万 Token</dd></div>
                    <div><dt>输出单价</dt><dd>{job.pricing.output_per_million_cny} 元／百万 Token</dd></div>
                    <div><dt>价格来源</dt><dd>{job.pricing.source || '未记录'}</dd></div>
                    <div><dt>核实时间</dt><dd>{job.pricing.verified_at || '未记录'}</dd></div>
                  </dl>}
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
                  {multipleChoice && <>
                    <div><dt>数据版本</dt><dd className="gsm8k-fingerprint">{job.dataset_revision}</dd></div>
                    <div><dt>题单指纹</dt><dd className="gsm8k-fingerprint">{job.manifest_sha256}</dd></div>
                    {job.scoring_version && <div><dt>评分版本</dt><dd>{job.scoring_version}</dd></div>}
                    <div><dt>抽样种子</dt><dd>{job.seed}</dd></div>
                    {!!job.pending_resume_count && <div><dt>待执行题目续跑</dt><dd>{job.pending_resume_count} 次</dd></div>}
                  </>}
                  {job.frontend_observations?.map((item, index) => <div key={`${item.event}-${item.observed_at}-${index}`}>
                    <dt>{item.event === 'created' ? '创建时前端' : item.event === 'started' ? '启动时前端' : '前端记录'}</dt>
                    <dd className="gsm8k-fingerprint">{item.sha256 ?? '未能读取'} · {item.observed_at}</dd>
                  </div>)}
                </dl>
                {!multipleChoice && <p className="gsm8k-hint">前端指纹记录的是后端本机源码，不代表浏览器实际加载的资源版本。</p>}
              </details>
            </div>
          </div>}
          {view === 'compare' && <div className="gsm8k-card-body">
            <p className="gsm8k-card-note">{job.methods.length} 种方法 · 每方法 {job.samples} 题</p>
            <div className="gsm8k-methods">{job.methods.map(method => {
              const rows = job.results.filter(row => row.method === method)
              const correct = rows.filter(row => row.scores.fixed?.correct === 1).length
              const finished = multipleChoice ? rows.filter(row => row.status === 'completed').length : rows.length
              const complete = multipleChoice ? finished === job.samples
                : rows.length === job.samples && rows.every(row => ['completed', 'failed', 'interrupted'].includes(row.status))
              const unresolved = multipleChoice ? rows.filter(row => ['failed', 'interrupted'].includes(row.status)).length : 0
              const scoreStatus = unresolved ? `有 ${unresolved} 道中断或失败题待核对` : '待整集完成'
              const metrics = job.method_metrics?.[method]
              const attempts = metrics?.calls ?? job.calls?.filter(call => call.task_key.startsWith(`${method}:`)).length ?? 0
              const durations = rows.flatMap(row => row.duration_ms == null ? [] : [row.duration_ms])
              return <div className="gsm8k-method" key={method}>
                <div className="gsm8k-method-head"><strong>{labels[method]}</strong><span>完成 {finished}/{job.samples} 题</span></div>
                <div className="gsm8k-method-score">{complete ? `${(correct * 100 / job.samples).toFixed(1)}%` : '—'}<small>{complete ? `${correct}/${job.samples} 题` : scoreStatus}</small></div>
                <span className="gsm8k-method-meta">生成记录 {attempts} 次</span>
                <span className="gsm8k-method-meta">固定评分：{complete ? `${correct}/${job.samples}` : scoreStatus}</span>
                {metrics && <>
                  <span className="gsm8k-method-meta">Token 入 / 出 {metrics.usage.prompt_tokens ?? '—'} / {metrics.usage.completion_tokens ?? '—'}</span>
                  <span className="gsm8k-method-meta">平均每题 {durations.length ? `${(durations.reduce((a, b) => a + b, 0) / durations.length / 1000).toFixed(2)} 秒` : '—'}</span>
                </>}
              </div>
            })}</div>
            {multipleChoice && job.quotas && <div className="gsm8k-results-scroll" role="region" tabIndex={0} aria-label="分学科答对数">
              <table className="gsm8k-results-table">
                <thead><tr><th scope="col">学科</th>{job.methods.map(method => <th scope="col" key={method}>{labels[method]}</th>)}</tr></thead>
                <tbody>{Object.entries(job.quotas).map(([category, quota]) =>
                  <tr key={category}><th scope="row">{subjectLabels[category] ?? category}</th>{job.methods.map(method => {
                    const rows = job.results.filter(row => row.method === method && row.category === category && row.status === 'completed')
                    return <td key={method}>{rows.length === quota
                      ? `${rows.filter(row => row.scores.fixed.correct === 1).length} / ${quota}`
                      : '—'}</td>
                  })}</tr>)}</tbody>
              </table>
            </div>}
            <small>{multipleChoice ? (job.fake ? '离线结果用于检验流程和评分。' : '准确率以固定题量为分母，整组结束后显示。')
              : `固定评分使用固定答案序列，需整集 ${job.samples} 题全部结束才汇报准确率；重试与中断题按原状态保留。`}</small>
          </div>}
          {view === 'results' && <QuestionResults key={job.job_id} job={job} multipleChoice={multipleChoice} />}
        </div>
      </section> : <section className="gsm8k-detail-panel gsm8k-empty-workspace">
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={jobs.length ? '从左侧选择一条实验记录查看结果' : '还没有实验记录，先创建一个实验'}>
          <Button type="primary" icon={<Plus size={14} aria-hidden="true" />} onClick={() => { setError(''); setCreateOpen(true) }}>新建实验</Button>
        </Empty>
      </section>}
    </div>

    <Drawer title="配置新实验" open={createOpen} onClose={() => setCreateOpen(false)} width="min(640px, 100vw)"
      className="gsm8k-create-drawer"
      footer={<div className="gsm8k-create-foot">
        <small>{selectionLabels[selection] ?? selection} · {sampleCount ?? '—'} 题 · {methods.length} 种方法 · 预计 {pendingCalls} 次正常生成{!multipleChoice && '，重试另计'}</small>
        <Button type="primary" disabled={busy || !name.trim() || !methods.length || (multipleChoice && !manifest?.count)} onClick={() => void action(create)}>创建实验</Button>
      </div>}>
      {createOpen && error && <Alert type="error" showIcon title={error} closable onClose={() => setError('')} className="gsm8k-drawer-alert" />}
      <div className="gsm8k-fields">
        <label>实验名称<Input aria-label={`${benchmarkName} 实验名称`} value={name} onChange={e => setName(e.target.value)} maxLength={100} /></label>
        <label>题目来源<Select aria-label={`${benchmarkName} 题目来源`} value={selection} onChange={value => {
          setSelection(value)
          if (multipleChoice) {
            if (value === 'calibration-60') setMethods(['single'])
            if (value === 'comparison-100') setMethods(allMethods)
            if (name === `MMLU-Pro ${selectionLabels[selection] ?? selection}`) setName(`MMLU-Pro ${selectionLabels[value] ?? value}`)
          } else {
            setCount(value === 'formal' ? 100 : 1)
            if (value === 'formal') { setFake(false); setMethods(allMethods) }
          }
        }} options={multipleChoice ? manifests.map(item => ({ value: item.selection, label: selectionLabels[item.selection] ?? item.selection })) : [
          { value: 'debug', label: '固定训练调试集（10 题）' }, { value: 'calibration', label: '固定训练校准集（30 题）' }, { value: 'custom', label: '导入纯问题 JSON' }, { value: 'formal', label: '正式测试集（100 题，需先冻结条件）' },
        ]} /></label>
        <label>题数<InputNumber aria-label={`${benchmarkName} 题数`} min={1} max={multipleChoice ? sampleCount : selection === 'debug' ? 10 : selection === 'calibration' ? 30 : 100} disabled={multipleChoice || selection === 'formal'} value={sampleCount} onChange={v => setCount(v ?? 1)} /></label>
        {multipleChoice && manifest && <div className="gsm8k-fields-wide">
          {manifest.error ? <Alert type="error" title={manifest.error} /> : <small>
            {Object.entries(manifest.quotas ?? {}).map(([category, quota]) => `${subjectLabels[category] ?? category} ${quota} 题`).join(' · ')}
            {' · '}{manifest.split} · 种子 {manifest.seed} · 数据版本 {manifest.revision?.slice(0, 12)}
          </small>}
        </div>}
        <fieldset><legend>复现方法</legend><Checkbox.Group disabled={selection === 'formal' || selection === 'comparison-100'} value={methods} onChange={v => setMethods(v as Method[])} options={Object.entries(labels).map(([value, label]) => ({ value, label }))} /></fieldset>
        {selection === 'comparison-100' && <div className="gsm8k-fields-wide"><small>先运行 Single，完成后自动暂停。确认继续后运行其余三组。</small></div>}
        {selection === 'formal' && <div className="gsm8k-fields-wide"><Alert type="info" showIcon title="正式测试需先在实验方案中冻结条件与模型文件。" /></div>}
        {selection === 'custom' && <>
          <label className="gsm8k-fields-wide">导入 JSON 文件<input type="file" aria-label="导入 GSM8K JSON 文件" accept=".json,application/json" onChange={e => { const file = e.target.files?.[0]; if (file) void action(() => importFile(file)); e.target.value = '' }} /></label>
          <label className="gsm8k-fields-wide">题目 JSON<Input.TextArea aria-label="GSM8K 题目 JSON" rows={5} value={custom} onChange={e => setCustom(e.target.value)} placeholder={'[{"question":"问题","answer":"数字答案"}]'} /><small>每题提供问题和数字参考答案；参考答案只用于评分。</small></label>
        </>}
        <div className="gsm8k-fields-wide gsm8k-check"><Checkbox checked={fake} disabled={selection === 'formal'} onChange={e => setFake(e.target.checked)}>使用固定假回答进行离线检查</Checkbox></div>
      </div>
    </Drawer>

    <Modal open={confirming} title={job?.fake ? '启动离线模拟？' : '确认调用真实模型？'} okText="确认启动" cancelText="取消" confirmLoading={busy}
      onCancel={() => setConfirming(false)} onOk={() => { if (!job) return; setConfirming(false); void action(async () => {
        const endpoint = resuming ? 'resume-pending' : 'start'
        await evaluationRequest(`${jobsPath}/${job.job_id}/${endpoint}`, 'POST', { allow_model_calls: !job.fake, retry_failed: false })
        await refreshJob(job.job_id); await refreshList()
      }) }}>
      <p>{job?.fake ? '本次使用固定假回答，不调用模型。' : `${job?.selection === 'formal' ? '本次为正式测试，题目及结果不能用于调试。' : ''}本次计划 ${multipleChoice ? nextStageCalls : job?.planned_calls ?? 0} 次正常模型生成${multipleChoice ? '' : '，重试另计'}，实际费用以供应商账单为准。请确认已核实当前价格和预算。`}</p>
      {resuming && <p>本次只续跑待执行题目。已完成结果和中断记录保留在这条实验中。</p>}
      {multipleChoice && nextStageCalls < remainingCalls && <p>本次只运行 Single，完成后自动暂停。全程计划 {job?.planned_calls} 次生成。</p>}
    </Modal>
  </section>
}
