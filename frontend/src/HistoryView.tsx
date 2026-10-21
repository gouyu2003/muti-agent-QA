import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import {
  ArrowRightOutlined,
  ClockCircleOutlined,
  DatabaseOutlined,
  DownloadOutlined,
  FileSearchOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { Alert, Button, Empty, Input, Pagination, Select, Skeleton, Tag, Tooltip } from 'antd'
import { getManagedSystems, getExperiments, getRunDetail, getRuns, getRunsExportUrl, getRunStats } from './api'
import EvidenceLocation from './EvidenceLocation'
import AgentResults from './AgentResults'
import ProtocolCalls from './ProtocolCalls'
import ModelDiagnostics, { ModelCalls } from './ModelDiagnostics'
import type {
  RunEntryPoint,
  RunFilters,
  RunListResponse,
  RunResult,
  RunStatsSummary,
  SourceOption,
  StoredRunDetail,
  StoredRunRecord,
  StoredRunStatus,
  StoredTraceEvent,
  ExperimentOption,
  ManagedSystem,
} from './types'

const PAGE_SIZE = 10
type TimeRange = 'all' | 'day' | 'week' | 'month'

const STATUS_LABELS: Record<StoredRunStatus, string> = {
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const NODE_LABELS: Record<string, string> = {
  planner_v0: '规划',
  retriever_v0: '检索',
  answer_validator_v0: '回答与验证',
}

const ROUTE_LABELS: Record<string, string> = {
  planner_v0: '重新规划',
  retriever_v0: '补充检索',
  __end__: '结束运行',
}

function isAbort(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '运行历史加载失败，请重试。'
}

function systemLabel(snapshot: Record<string, unknown>): string {
  const ref = asObject(snapshot.system_ref)
  return typeof ref.system_id === 'string' ? `${String(ref.name)} · ${ref.kind === 'draft' ? `草稿 r${String(ref.draft_revision)}` : String(ref.system_version)}` : ''
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function formatDuration(value: number | null): string {
  if (value === null) return '—'
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`
}

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function eventHeading(event: StoredTraceEvent): string {
  const node = typeof event.payload.agent_name === 'string' ? event.payload.agent_name : NODE_LABELS[event.node ?? ''] ?? event.node ?? '运行'
  if (event.event_type === 'node_started') return `${node}开始`
  if (event.event_type === 'node_completed') return `${node}完成`
  if (event.event_type === 'route_decided') return '路由决定'
  if (event.event_type === 'run_started') return '运行开始'
  if (event.event_type === 'run_completed') return '运行完成'
  if (event.event_type === 'run_failed') return '运行失败'
  if (event.event_type === 'run_cancelled') return '运行取消'
  return event.event_type
}

function EventBody({ event }: { event: StoredTraceEvent }) {
  const payload = event.payload
  if (event.event_type === 'route_decided') {
    const target = String(payload.target ?? '')
    return (
      <p className="history-event-note">
        <ArrowRightOutlined /> {ROUTE_LABELS[target] ?? target} · {String(payload.reason ?? '无路由说明')}
      </p>
    )
  }
  if (event.event_type === 'run_failed' || event.event_type === 'run_cancelled') {
    return <p className="history-event-note">{String(payload.message ?? payload.reason ?? '运行未完成')}</p>
  }
  if (event.event_type !== 'node_completed') return null

  const output = asObject(payload.output) as Partial<RunResult>
  if (output.protocol_calls) return <ProtocolCalls calls={output.protocol_calls} />
  if (event.node === 'planner_v0' || payload.agent_type === 'planner') {
    return (
      <div className="history-event-detail">
        {(output.sub_questions ?? []).map((item) => (
          <p key={item.id}><code>{item.id}</code> {item.question}</p>
        ))}
        {output.retrieval_strategy ? <small>检索策略：{output.retrieval_strategy}</small> : null}
        <ModelDiagnostics diagnostics={output.model_diagnostics} />
      </div>
    )
  }
  if (event.node === 'retriever_v0' || payload.agent_type === 'retriever') {
    const groups = output.evidence_chunks ?? []
    const count = groups.reduce((sum, group) => sum + group.chunks.length, 0)
    return <p className="history-event-note">覆盖 {groups.length} 个子问题 · 返回 {count} 条候选证据</p>
  }
  if (output.expert_results) return <AgentResults results={output.expert_results} />
  if (event.node === 'answer_validator_v0' || payload.agent_type === 'answer_validator') {
    return (
      <div className="history-event-note">
        {output.verified ? '验证通过' : `验证未通过：${output.failure_type ?? '原因未记录'}`}
        {output.verification_note ? ` · ${output.verification_note}` : ''}
        <ModelDiagnostics diagnostics={output.model_diagnostics} />
      </div>
    )
  }
  return null
}

function RunTimeline({ events }: { events: StoredTraceEvent[] }) {
  return (
    <section className="history-timeline" aria-label="运行事件时间线">
      <div className="history-section-title">
        <span className="section-kicker">EXECUTION TAPE</span>
        <h3>执行轨迹</h3>
        <span>{events.length} 个事件</span>
      </div>
      {events.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此运行暂无轨迹事件" />
      ) : (
        <ol className="history-events">
          {events.map((event) => (
            <li key={event.sequence} className={`history-event history-event--${event.event_type}`}>
              <span className="history-event-index">{String(event.sequence).padStart(2, '0')}</span>
              <div>
                <div className="history-event-topline">
                  <strong>{eventHeading(event)}</strong>
                  <span>{formatDate(event.timestamp)}</span>
                </div>
                {event.cycle ? <small>第 {event.cycle} 轮</small> : null}
                {event.duration_ms !== null ? <small> · {formatDuration(event.duration_ms)}</small> : null}
                <EventBody event={event} />
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function RunEvidence({ result, source }: { result: RunResult | null; source?: Pick<SourceOption, 'source_id' | 'name'> }) {
  const groups = result?.evidence_chunks ?? []
  const supporting = result?.supporting_evidence ?? []
  const items = supporting.length
    ? supporting.map((item, index) => ({
        id: `${item.sq_id}-${index}`, sqId: item.sq_id,
        title: item.source_title, text: item.text, sourceId: item.source_id, evidence: item,
      }))
    : groups.flatMap((group) => group.chunks.map((chunk, index) => ({
        id: `${group.sq_id}-${index}`, sqId: group.sq_id,
        title: chunk.source_title, text: chunk.text, sourceId: chunk.source_id, evidence: chunk,
      })))

  return (
    <section className="history-evidence" aria-label="运行证据">
      <div className="history-section-title">
        <span className="section-kicker">EVIDENCE LEDGER</span>
        <h3>{supporting.length ? '支撑证据' : '检索证据'}</h3>
        <span>{items.length} 条</span>
      </div>
      {items.length ? items.map((item) => (
        <article key={item.id} className="history-evidence-item">
          <span><DatabaseOutlined /> {item.sqId} · {item.title}{item.sourceId ? ` · ${source?.source_id === item.sourceId ? source.name : item.sourceId}` : ''}</span>
          <EvidenceLocation evidence={item.evidence} source={source} />
          <p>{item.text}</p>
        </article>
      )) : <p className="history-muted">此运行没有保存可展示的证据。</p>}
    </section>
  )
}

function RunDetailPanel({ detail, experimentName }: { detail: StoredRunDetail; experimentName?: string }) {
  const { run, events } = detail
  const result = run.final_state
  const snapshot = asObject(run.context.config_snapshot)
  const sourceSnapshot = asObject(snapshot.source)
  const sourceId = run.context.source_id ?? (typeof sourceSnapshot.source_id === 'string' ? sourceSnapshot.source_id : undefined)
  const sourceName = typeof sourceSnapshot.name === 'string' ? sourceSnapshot.name.trim() : ''
  const source = sourceId && sourceName ? { source_id: sourceId, name: sourceName } : undefined
  const systemVersion = typeof snapshot.system_version === 'string' ? snapshot.system_version : '未记录'
  const configHash = typeof snapshot.config_sha256 === 'string' ? snapshot.config_sha256 : '未记录'
  const sourceHash = typeof sourceSnapshot.sha256 === 'string' ? sourceSnapshot.sha256 : '未记录'
  return (
    <article className="history-detail">
      <div className="history-detail-head">
        <span className="section-kicker">RUN / {run.context.run_id}</span>
        <Tag className={`history-status history-status--${run.status}`}>
          {STATUS_LABELS[run.status]}
        </Tag>
      </div>
      <h2>{run.context.question}</h2>
      <div className="history-detail-meta">
        <span>{formatDate(run.started_at)}</span>
        <span>{run.context.entrypoint.toUpperCase()}</span>
        <span>{run.context.method_id}</span>
        <span>{run.context.model_name}</span>
      </div>
      <div className="history-config-facts" aria-label="生效配置">
        {run.context.experiment_id ? <>
          <div><span>实验</span><strong>{experimentName || run.context.experiment_id}</strong></div>
          <div><span>实验 ID</span><code>{run.context.experiment_id}</code></div>
        </> : null}
        {run.context.sample_id ? <div><span>样本编号</span><code>{run.context.sample_id}</code></div> : null}
        {systemLabel(snapshot) ? <><div><span>所属问答系统</span><strong>{systemLabel(snapshot)}</strong></div><div><span>系统 ID</span><code>{String(asObject(snapshot.system_ref).system_id)}</code></div></> : null}
        <div><span>协作流程</span><strong>{run.context.workflow_id}</strong></div>
        <div><span>方法</span><strong>{run.context.method_id}</strong></div>
        <div><span>知识源</span><strong>{sourceName || sourceId || (Object.keys(asObject(snapshot.sources)).length ? '多个固定知识源' : '未记录')}</strong></div>
        {sourceId ? <div><span>知识源 ID</span><code>{sourceId}</code></div> : null}
        {typeof sourceSnapshot.version === 'number' ? <div><span>知识源版本</span><strong>{sourceSnapshot.version}</strong></div> : null}
        {run.context.record_id ? <div><span>材料记录</span><strong>{run.context.record_id}</strong></div> : null}
        <div><span>系统配置版本</span><strong>{systemVersion}</strong></div>
        <div><span>代码版本</span><code>{run.context.code_version}</code></div>
        <div><span>配置摘要 SHA-256</span><code>{configHash}</code></div>
        <div><span>知识源 SHA-256</span><code>{sourceHash}</code></div>
      </div>
      {['2', '3'].includes(String(snapshot.schema_version)) ? <section className="history-instance-config"><h3>本次智能体配置</h3>
        {Object.entries(asObject(snapshot.agents)).map(([id, value]) => {
          const a = asObject(value), model = asObject(a.model_config), s = asObject(asObject(snapshot.sources)[id])
          const usage = run.metrics.agent_usage?.[id]
          return <details key={id}><summary>{String(a.agent_name)} · {String(a.role)} · {String(model.model_name ?? '本地 BM25')}</summary>
            <p>实例：{id} · 温度 {String(model.temperature ?? '—')} · Token 上限 {String(model.max_tokens ?? '—')}</p>
            {s.source_id ? <p>材料：{String(s.name)} · 版本 {String(s.version ?? '固定内容')} · SHA-256 <code>{String(s.sha256)}</code></p> : null}
            {model.system_prompt ? <p className="system-prompt">{String(model.system_prompt)}</p> : null}
            {usage ? <p>LLM 调用 {usage.call_count} · Token {usage.total_tokens ?? '未知'}</p> : null}
            <ModelCalls calls={usage?.calls} />
          </details>
        })}
      </section> : null}
      <div className="history-run-facts">
        <div><span>耗时</span><strong>{formatDuration(run.metrics.duration_ms)}</strong></div>
        <div><span>节点调用</span><strong>{run.metrics.node_call_count}</strong></div>
        <div><span>LLM 调用</span><strong>{run.metrics.llm_call_count ?? '—'}</strong></div>
        <div><span>总 Token</span><strong>{run.metrics.total_tokens ?? '—'}</strong></div>
      </div>
      {run.error ? (
        <Alert type="error" showIcon message={run.error.error_type} description={run.error.message} />
      ) : null}
      {run.status === 'cancelled' ? (
        <Alert type="info" showIcon message="此运行已取消，以下为取消前保存的轨迹。" />
      ) : null}
      <section className="history-answer">
        <div className="history-section-title">
          <span className="section-kicker">FINAL RESPONSE</span>
          <h3>最终答案</h3>
          {run.status === 'completed' ? (
            <Tag color={result?.verified ? 'success' : 'warning'}>
              {result?.verified ? '验证通过' : '验证未通过'}
            </Tag>
          ) : null}
        </div>
        <p className="history-answer-text">{result?.answer || '未生成可用答案'}</p>
        <p className="history-muted">
          {result?.verification_note || (run.status === 'running' ? '运行仍在进行中。' : '没有保存验证说明。')}
        </p>
        {result?.failure_type && result.failure_type !== 'none' ? (
          <p className="history-failure-type">失败类型：{result.failure_type}</p>
        ) : null}
      </section>
      <RunTimeline events={events} />
      <AgentResults results={result?.expert_results} />
      <ProtocolCalls calls={result?.protocol_calls} />
      <RunEvidence result={result} source={source} />
    </article>
  )
}

export default function HistoryView() {
  const [searchParams, setSearchParams] = useSearchParams()
  const systemId = searchParams.get('systemId') || undefined
  const [systems, setSystems] = useState<ManagedSystem[]>([])
  const [systemError, setSystemError] = useState('')
  const [status, setStatus] = useState<StoredRunStatus | undefined>()
  const [entrypoint, setEntrypoint] = useState<RunEntryPoint | undefined>()
  const [experiments, setExperiments] = useState<ExperimentOption[]>([])
  const [experimentId, setExperimentId] = useState<string>()
  const [experimentError, setExperimentError] = useState('')
  const [timeRange, setTimeRange] = useState<TimeRange>('all')
  const [methodDraft, setMethodDraft] = useState('')
  const [methodId, setMethodId] = useState('')
  const [page, setPage] = useState(1)
  const [refresh, setRefresh] = useState(0)
  const [listing, setListing] = useState<RunListResponse>()
  const [stats, setStats] = useState<RunStatsSummary>()
  const [selectedId, setSelectedId] = useState<string>()
  const [detail, setDetail] = useState<StoredRunDetail>()
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [listError, setListError] = useState('')
  const [detailError, setDetailError] = useState('')

  useEffect(() => {
    setPage(1)
    setSelectedId(undefined)
  }, [systemId])

  const startedFrom = useMemo(() => {
    const hours: Record<Exclude<TimeRange, 'all'>, number> = {
      day: 24, week: 24 * 7, month: 24 * 30,
    }
    return timeRange === 'all' ? undefined : new Date(Date.now() - hours[timeRange] * 3600000).toISOString()
  }, [timeRange, refresh])
  const filters = useMemo<RunFilters>(
    () => ({ status, entrypoint, methodId: methodId || undefined, startedFrom, experimentId, systemId }),
    [status, entrypoint, methodId, startedFrom, experimentId, systemId],
  )

  useEffect(() => {
    const controller = new AbortController()
    setExperimentError('')
    setSystemError('')
    getManagedSystems(controller.signal).then(data => setSystems(data.items)).catch((error: unknown) => { if (!isAbort(error)) setSystemError(errorText(error)) })
    getExperiments(controller.signal).then(setExperiments).catch((error: unknown) => {
      if (!isAbort(error)) { setExperiments([]); setExperimentError(errorText(error)) }
    })
    return () => controller.abort()
  }, [refresh])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setListError('')
    Promise.all([
      getRuns(filters, PAGE_SIZE, (page - 1) * PAGE_SIZE, controller.signal),
      getRunStats(filters, controller.signal),
    ]).then(([runs, summary]) => {
      setListing(runs)
      setStats(summary)
      setSelectedId((current) =>
        runs.items.some((item) => item.context.run_id === current)
          ? current : runs.items[0]?.context.run_id,
      )
    }).catch((error: unknown) => {
      if (!isAbort(error)) {
        setListing(undefined)
        setStats(undefined)
        setSelectedId(undefined)
        setListError(errorText(error))
      }
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [filters, page, refresh])

  useEffect(() => {
    if (!selectedId) {
      setDetail(undefined)
      setDetailLoading(false)
      setDetailError('')
      return
    }
    const controller = new AbortController()
    setDetail(undefined)
    setDetailError('')
    setDetailLoading(true)
    getRunDetail(selectedId, controller.signal, experimentId)
      .then(setDetail)
      .catch((error: unknown) => {
        if (!isAbort(error)) setDetailError(errorText(error))
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false)
      })
    return () => controller.abort()
  }, [selectedId, refresh, experimentId])

  return (
    <main className="history-workspace">
      <header className="history-hero">
        <div>
          <span className="section-kicker">RUN ARCHIVE</span>
          <h1>每次运行，都有迹可循</h1>
          <p>查看日常问答与批量实验的问题、答案、证据和执行轨迹。</p>
        </div>
        <div className="history-hero-actions">
          <Button icon={<ReloadOutlined />} onClick={() => setRefresh((value) => value + 1)}>
            刷新
          </Button>
          <Tooltip title="导出当前筛选条件下的全部运行">
            <Button icon={<DownloadOutlined />} href={getRunsExportUrl(filters)}>
              导出 JSONL
            </Button>
          </Tooltip>
        </div>
      </header>

      {listError ? (
        <Alert
          type="error" showIcon message="运行历史加载失败" description={listError}
          action={<Button size="small" onClick={() => setRefresh((value) => value + 1)}>重试</Button>}
        />
      ) : null}

      <section className="history-stats" aria-label="运行统计">
        <div><span>运行总数</span><strong>{loading ? '—' : stats?.total_runs ?? 0}</strong><small>当前筛选范围内的运行</small></div>
        <div><span>完成率</span><strong>{loading ? '—' : formatPercent(stats?.completion_rate ?? 0)}</strong><small>已完成 / 全部运行</small></div>
        <div><span>验证通过率</span><strong>{loading ? '—' : formatPercent(stats?.verification_pass_rate ?? 0)}</strong><small>已验证 / 已完成，非答案准确率</small></div>
        <div><span>平均耗时</span><strong>{loading ? '—' : formatDuration(stats?.average_duration_ms ?? null)}</strong><small>有耗时记录的运行</small></div>
      </section>

      {systemError ? <Alert type="error" title="系统筛选加载失败" description={systemError} /> : null}
      <section className="history-filters" aria-label="筛选运行历史">
        <div className="history-filter-heading">
          <FileSearchOutlined />
          <strong>筛选档案</strong>
        </div>
        <Select
          aria-label="状态筛选" value={status ?? 'all'}
          onChange={(value) => {
            setStatus(value === 'all' ? undefined : value as StoredRunStatus)
            setPage(1)
          }}
          options={[
            { value: 'all', label: '全部状态' },
            ...Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label })),
          ]}
        />
        <Select
          aria-label="入口筛选" value={entrypoint ?? 'all'}
          onChange={(value) => {
            setEntrypoint(value === 'all' ? undefined : value as RunEntryPoint)
            setExperimentId(undefined)
            setSelectedId(undefined)
            setPage(1)
          }}
          options={[
            { value: 'all', label: '全部入口' },
            { value: 'api', label: 'API / 前端' },
            { value: 'cli', label: 'CLI / 终端' },
            { value: 'batch', label: '批量实验' },
          ]}
        />
        <Select
          aria-label="时间筛选" value={timeRange}
          onChange={(value) => { setTimeRange(value); setPage(1) }}
          options={[
            { value: 'all', label: '全部时间' },
            { value: 'day', label: '最近 24 小时' },
            { value: 'week', label: '最近 7 天' },
            { value: 'month', label: '最近 30 天' },
          ]}
        />
        <Input.Search
          aria-label="方法 ID 筛选" placeholder="按方法 ID 筛选" value={methodDraft}
          onChange={(event) => {
            setMethodDraft(event.target.value)
            if (!event.target.value) { setMethodId(''); setPage(1) }
          }}
          onSearch={(value) => { setMethodId(value.trim()); setPage(1) }}
          allowClear enterButton="筛选"
        />
        <Select className="history-experiment-select" aria-label="系统筛选" value={systemId ?? ''}
          onChange={value => {
            setSearchParams(current => {
              const next = new URLSearchParams(current)
              if (value) next.set('systemId', value)
              else next.delete('systemId')
              return next
            })
            setPage(1)
            setSelectedId(undefined)
          }}
          options={[{ value: '', label: '全部系统与临时问答' }, ...systems.map(item => ({ value: item.system_id, label: item.draft.name }))]} />
        {entrypoint === 'batch' ? <Select
          className="history-experiment-select"
          aria-label="实验筛选" value={experimentId ?? ''}
          onChange={(value) => { setExperimentId(value || undefined); setSelectedId(undefined); setPage(1) }}
          options={[{ value: '', label: '全部批量实验' }, ...experiments.map((item) => ({ value: item.experiment_id, label: item.name }))]}
        /> : null}
      </section>
      {experimentError ? <Alert type="error" showIcon title="实验档案加载失败" description={experimentError} /> : null}

      <div className="history-main-grid">
        <section className="history-list-panel" aria-label="历史运行列表">
          <div className="history-panel-head">
            <div><span className="section-kicker">RUN INDEX</span><h2>历史运行</h2></div>
            <span>{loading ? '加载中' : `${listing?.total ?? 0} 条`}</span>
          </div>
          {loading ? <Skeleton active paragraph={{ rows: 8 }} /> : null}
          {!loading && !listError && !listing?.items.length ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={stats?.total_runs === 0 && !status && !entrypoint && !methodId && timeRange === 'all'
                ? '还没有运行记录。先去实时运行发起一次问答。'
                : '没有符合筛选条件的运行。试试放宽状态、方法或时间范围。'}
            />
          ) : null}
          {!loading && !listError ? (
            <div className="history-run-list">
              {listing?.items.map((item: StoredRunRecord) => (
                <button
                  key={item.context.run_id} type="button"
                  className={`history-run-row ${selectedId === item.context.run_id ? 'selected' : ''}`}
                  onClick={() => setSelectedId(item.context.run_id)}
                  aria-pressed={selectedId === item.context.run_id}
                >
                  <span className="history-run-row-top">
                    <span className={`history-status-dot history-status-dot--${item.status}`} />
                    <span>{formatDate(item.started_at)}</span>
                    <Tag className={`history-status history-status--${item.status}`}>
                      {STATUS_LABELS[item.status]}
                    </Tag>
                  </span>
                  <strong>{item.context.question}</strong>
                  {systemLabel(item.context.config_snapshot) ? <span className="history-experiment-label">{systemLabel(item.context.config_snapshot)}</span> : null}
                  {item.context.experiment_id ? <span className="history-experiment-label">
                    {experiments.find((experiment) => experiment.experiment_id === item.context.experiment_id)?.name || item.context.experiment_id}
                    {item.context.sample_id ? ` · 样本 ${item.context.sample_id}` : ''}
                  </span> : null}
                  <span className="history-run-row-bottom">
                    {item.context.entrypoint.toUpperCase()} · {item.context.method_id}
                    <span><ClockCircleOutlined /> {formatDuration(item.metrics.duration_ms)}</span>
                  </span>
                </button>
              ))}
            </div>
          ) : null}
          {!loading && !listError && (listing?.total ?? 0) > PAGE_SIZE ? (
            <Pagination
              className="history-pagination" size="small" current={page}
              total={listing?.total ?? 0} pageSize={PAGE_SIZE} showSizeChanger={false}
              onChange={(value) => setPage(value)}
            />
          ) : null}
        </section>

        <section className="history-detail-panel" aria-label="运行详情">
          {detailLoading || loading ? <Skeleton active paragraph={{ rows: 12 }} /> : null}
          {detailError && !loading ? (
            <Alert type="error" showIcon message="运行详情加载失败" description={detailError} />
          ) : null}
          {!loading && !detailLoading && !detailError && detail ? <RunDetailPanel detail={detail}
            experimentName={experiments.find((experiment) => experiment.experiment_id === detail.run.context.experiment_id)?.name} /> : null}
          {!loading && !detailLoading && !detailError && !detail && !listError ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择一条运行，查看完整轨迹与答案。" />
          ) : null}
        </section>
      </div>
      <p className="history-footer-note">
        验证通过率反映验证器的结论，不等同于标准答案准确率。
      </p>
    </main>
  )
}
