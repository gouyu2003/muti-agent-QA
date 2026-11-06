import { Collapse, Tag, Typography } from 'antd'
import { TimingDetails, RunDiagnostics } from './RunInsights'
import ProtocolCalls from './ProtocolCalls'
import type { StoredRunDetail, StoredTraceEvent } from './types'

const STATUS: Record<string, string> = { completed: '流程完成', failed: '任务失败', running: '运行中', cancelled: '已取消', interrupted: '状态不明' }
const EVENTS: Record<string, string> = { run_started: '运行开始', node_started: '节点开始', node_completed: '节点完成',
  route_decided: '路由决定', run_completed: '运行完成', run_failed: '运行失败', run_cancelled: '运行取消' }

function date(value?: string | null) {
  if (!value) return '未记录'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { hour12: false })
}

function eventNote(event: StoredTraceEvent) {
  const payload = event.payload
  const text = event.event_type === 'route_decided' ? payload.reason : payload.message ?? payload.reason
  return typeof text === 'string' ? text : ''
}

export default function EvaluationRunDetail({ detail }: { detail: StoredRunDetail }) {
  const { run, events } = detail
  const calls = run.final_state?.protocol_calls
  return <article className="evaluation-run-detail">
    <header><Tag color={run.status === 'failed' ? 'error' : run.status === 'completed' ? 'success' : 'default'}>{STATUS[run.status] ?? run.status}</Tag>
      <h3>{run.context.question}</h3>
      <p>{run.context.method_id} · {run.context.model_name}</p></header>
    <dl className="evaluation-run-meta">
      <div><dt>运行 ID</dt><dd><Typography.Text copyable={{ tooltips: ['复制运行 ID', '已复制'] }}>{run.context.run_id}</Typography.Text></dd></div>
      <div><dt>问题 ID</dt><dd>{run.context.sample_id || '未记录'}</dd></div>
      <div><dt>开始时间</dt><dd>{date(run.started_at)}</dd></div>
      <div><dt>结束时间</dt><dd>{date(run.finished_at)}</dd></div>
      <div><dt>模型调用</dt><dd>{run.metrics.llm_call_count ?? '未记录'} 次</dd></div>
      <div><dt>总 Token</dt><dd>{run.metrics.total_tokens?.toLocaleString() ?? '未知'}</dd></div>
    </dl>
    <RunDiagnostics run={run} />
    <TimingDetails timings={run.metrics.timings} />
    <section><h3>最终答案</h3><p className="evaluation-final-answer">{run.final_state?.answer || '未生成可用答案'}</p>
      {run.final_state?.verification_note && <p className="diagnostic-note">{run.final_state.verification_note}</p>}</section>
    <Collapse items={[
      ...(calls && Object.keys(calls).length ? [{ key: 'protocol', label: '协作阶段详情', children: <ProtocolCalls calls={calls} /> }] : []),
      { key: 'events', label: `执行时间线（${events.length} 个事件）`, children: events.length
        ? <ol className="evaluation-event-list">{events.map(event => <li key={event.sequence}>
          <strong>{EVENTS[event.event_type] ?? event.event_type}{event.node ? ` · ${event.node}` : ''}</strong>
          <time>{date(event.timestamp)}{event.duration_ms != null ? ` · ${(event.duration_ms / 1000).toFixed(1)} 秒` : ''}</time>
          {eventNote(event) && <p>{eventNote(event)}</p>}
        </li>)}</ol> : <p>未记录执行事件。</p> },
      { key: 'raw', label: '原始 JSON（高级详情）', children: <pre className="evaluation-trace">{JSON.stringify(detail, null, 2)}</pre> },
    ]} />
  </article>
}
