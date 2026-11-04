import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Collapse, Input, Modal, Select, Space, Table, Tag } from 'antd'
import { evaluationRequest, type EvaluationJob, type EvaluationResult, type EvaluationMonitor } from './evaluationApi'
import { runDiagnosticSummary, errorTypeLabel } from './diagnosticSummary'
import { targetLabel } from './evaluationLabels'

export default function EvaluationReview({ job, base, onOpenDetail }: {
  job: EvaluationJob; base: string; onOpenDetail: (row: EvaluationResult) => void
}) {
  const [view, setView] = useState('first')
  const [status, setStatus] = useState<string>()
  const [method, setMethod] = useState<string>()
  const [label, setLabel] = useState<string>()
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState<EvaluationResult[]>([])
  const [total, setTotal] = useState(0)
  const [labels, setLabels] = useState<string[]>([])
  const [monitor, setMonitor] = useState<EvaluationMonitor>()
  const [error, setError] = useState('')
  const [comparison, setComparison] = useState<{ question: string; gold: unknown; items: (Omit<EvaluationResult, 'run_id'> & { run_id: string | null })[] }>()
  const [comparing, setComparing] = useState(false)
  const [comparisonError, setComparisonError] = useState('')
  const comparisonRequest = useRef(0)
  const name = (id: string) => targetLabel(job, id)
  const params = new URLSearchParams({ view, offset: String((page - 1) * 20), limit: '20' })
  if (status) params.set('status', status)
  if (method) params.set('method', method)
  if (label) params.set('label', label)
  if (search) params.set('search', search)
  const query = params.toString()
  useEffect(() => {
    let disposed = false
    async function load() {
      try {
        const [result, live] = await Promise.all([
          evaluationRequest<{ items: EvaluationResult[]; total: number; labels?: string[] }>(`${base}/results?${query}`),
          job.readonly ? Promise.resolve(undefined) : evaluationRequest<EvaluationMonitor>(`${base}/monitor`).catch(() => undefined),
        ])
        if (!disposed) { setRows(result.items ?? []); setTotal(result.total ?? 0); setLabels(result.labels ?? []); setMonitor(live); setError('') }
      } catch (e) { if (!disposed) setError(e instanceof Error ? e.message : String(e)) }
    }
    void load()
    const timer = job.readonly ? undefined : window.setInterval(() => void load(), 3000)
    return () => { disposed = true; if (timer !== undefined) window.clearInterval(timer) }
  }, [base, query, job.readonly])
  useEffect(() => () => { comparisonRequest.current += 1 }, [])
  useEffect(() => { comparisonRequest.current += 1; setComparing(false); setComparison(undefined); setComparisonError('') }, [view])
  function closeComparison() { comparisonRequest.current += 1; setComparing(false); setComparison(undefined); setComparisonError('') }
  async function compare(row: EvaluationResult) {
    const request = ++comparisonRequest.current
    setComparing(true); setComparison(undefined); setComparisonError('')
    try {
      const result = await evaluationRequest<NonNullable<typeof comparison>>(`${base}/compare?${new URLSearchParams({ sample_id: row.sample_id, repeat: String(row.repeat), view })}`)
      if (request === comparisonRequest.current) setComparison(result)
    } catch (e) { if (request === comparisonRequest.current) setComparisonError(e instanceof Error ? e.message : String(e)) }
  }
  function filter(fn: () => void) { fn(); setPage(1); setRows([]) }
  return <section aria-label="运行监控与结果审阅">
    {!job.readonly && <>
      <h3>运行监控</h3>
      {monitor?.current ? <div role="status" aria-label="当前运行状态">
        <p>当前对象：{name(monitor.current.method_id)}；题目 {monitor.current.sample_id}；重复 {monitor.current.repeat + 1}；尝试 {monitor.current.attempt}</p>
        <p>{monitor.current.question}</p>
        <p>阶段：{monitor.current.phases.map(p => [p.stage_id, p.agent_name ?? p.node].filter(Boolean).join(' · ')).join('、') ||
          (monitor.current.status === 'running' ? '初始化执行' : '终态已保存，正在汇总')}；本题已耗时 {monitor.current.elapsed_seconds.toFixed(1)} 秒</p>
        <small>最近轨迹事件：{monitor.last_event_at ? new Date(monitor.last_event_at).toLocaleString() : '尚未记录'}；事件间隔不等同于模型进程失活。</small>
      </div> : <p>{['queued', 'running', 'stopping'].includes(job.status) ? '正在等待后台初始化、派发或汇总；尚无可显示的当前题目。' : '任务当前未运行。'}</p>}
      {monitor?.methods?.length ? <>
        <p>已结束尝试累计耗时：{monitor.completed_attempt_seconds.toFixed(1)} 秒；未开始首次任务的估计剩余：{monitor.estimated_remaining_seconds == null ? '样本不足' : `${monitor.estimated_remaining_seconds.toFixed(0)} 秒`}。</p>
        <small>估算依据已记录首次尝试的平均耗时，不包含补跑、暂停等待及报告生成。</small>
        <Table size="small" rowKey="method_id" pagination={false} dataSource={monitor.methods} columns={[
          { title: '对象', dataIndex: 'method_id', render: name }, { title: '计划', dataIndex: 'planned' },
          { title: '首次完成', dataIndex: 'completed' }, { title: '首次失败', dataIndex: 'failed' },
          { title: '首次执行中', dataIndex: 'in_progress' },
          { title: '中断／取消', dataIndex: 'unresolved' }, { title: '未开始', dataIndex: 'unstarted' },
        ]} scroll={{ x: 650 }} />
      </> : null}
    </>}
    {(job.report?.retry_count ?? 0) > 0 && <Collapse items={[{ key: 'retries', label: `补跑补充统计（${job.report!.retry_count} 次新尝试，不替换主成绩）`, children: <>
      <Table size="small" pagination={false} rowKey="method" dataSource={Object.entries(job.report?.latest_attempt_supplement ?? {}).map(([method, data]) => ({ method, ...data }))} columns={[
        { title: '对象', dataIndex: 'method', render: name }, { title: '最新尝试完成', render: (_, r) => r.statuses.completed },
        { title: '最新尝试失败', render: (_, r) => r.statuses.failed }, { title: '最新 F1（补充）', render: (_, r) => r.scores.f1?.value == null ? '未知' : `${(r.scores.f1.value * 100).toFixed(1)}%` },
      ]} />
      <p>所有尝试已知 Token：{job.report?.all_attempt_usage?.total_tokens?.reported_total ?? '未知'}；所有尝试模型调用：{job.report?.all_attempt_usage?.llm_call_count?.reported_total ?? '未知'}。汇总来自最近生成的报告。</p>
    </> }]} />}
    <h3>逐题结果审阅</h3>
    <Alert type="info" title={view === 'first' ? '当前查看首次尝试，与主成绩一致。' : view === 'latest' ? '当前查看最新尝试，仅作补跑补充；主成绩仍按首次尝试统计。' : '当前查看全部尝试；不能混合取最好成绩或作为主表分母。'} />
    <Space wrap>
      <Select aria-label="尝试范围" value={view} onChange={v => filter(() => { setView(v); setLabel(undefined) })} options={[{ value: 'first', label: '首次尝试（主结果）' }, { value: 'latest', label: '最新尝试（补充）' }, { value: 'all', label: '全部尝试' }]} />
      <Select aria-label="结果状态" allowClear placeholder="全部状态" value={status} onChange={v => filter(() => setStatus(v))} options={[
        ['completed', '完成'], ['failed', '失败'], ['interrupted', '状态不明'], ['cancelled', '取消'],
      ].map(([value, label]) => ({ value, label }))} />
      <Select aria-label="结果方法" allowClear placeholder="全部对象" value={method} onChange={v => filter(() => { setMethod(v); setLabel(undefined) })} options={job.targets.map(t => ({ value: t.method_id, label: name(t.method_id) }))} />
      <Select aria-label="诊断类别" allowClear placeholder="全部诊断类别" value={label} onChange={v => filter(() => setLabel(v))} options={labels.map(value => ({ value, label: errorTypeLabel(value) }))} />
      <Input.Search aria-label="搜索题目或答案" placeholder="题目 ID、问题或答案" allowClear onSearch={v => filter(() => setSearch(v))} />
    </Space>
    {error && <Alert type="error" title={error} />}
    <Table size="small" rowKey="run_id" dataSource={rows} scroll={{ x: 950 }} pagination={{ current: page, total, pageSize: 20, onChange: setPage }}
      columns={[{ title: '问题', dataIndex: 'question', ellipsis: true }, { title: '对象', dataIndex: 'method_id', render: name },
        { title: '重复', dataIndex: 'repeat', render: r => r + 1 }, { title: '尝试', dataIndex: 'attempt' }, { title: '状态', dataIndex: 'status' },
        { title: '答案', dataIndex: 'answer', ellipsis: true }, { title: '运行诊断', render: (_, r) => {
          const summary = runDiagnosticSummary(r)
          return summary.hasIssues ? <Tag color={summary.color}>{summary.labels.join('、') || summary.heading}</Tag> : '—'
        } }, { title: '审阅', render: (_, r) => <Space wrap><Button size="small" onClick={() => void compare(r)}>同题答案对照</Button>
          <Button size="small" onClick={() => onOpenDetail(r)}>查看诊断与轨迹</Button></Space> }]} />
    <Modal title="同题答案对照" open={comparing} onCancel={closeComparison} footer={null} width={1100}>
      {comparisonError && <Alert type="error" title={comparisonError} />}
      {!comparison && !comparisonError && <p role="status">正在加载同题结果…</p>}
      {comparison && <>
        <p>{comparison.question}</p><p>标准答案：{typeof comparison.gold === 'object' ? JSON.stringify(comparison.gold) : String(comparison.gold ?? '未提供')}</p>
        <Table size="small" rowKey={r => r.run_id ?? r.method_id} dataSource={comparison.items} pagination={false} scroll={{ x: 900 }} columns={[
          { title: '对象', dataIndex: 'method_id', render: name }, { title: '尝试', dataIndex: 'attempt' }, { title: '状态', dataIndex: 'status' },
          { title: '完整答案', dataIndex: 'answer', render: v => <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{v || '尚无答案'}</div> },
          { title: 'F1', render: (_, r) => r.scores?.f1 == null ? '未知' : `${(r.scores.f1 * 100).toFixed(1)}%` },
          { title: '轨迹', render: (_, r) => r.run_id ? <Button size="small" onClick={() => { closeComparison(); onOpenDetail(r as EvaluationResult) }}>查看该答案轨迹</Button> : '尚无运行记录' },
        ]} />
      </>}
    </Modal>
  </section>
}
