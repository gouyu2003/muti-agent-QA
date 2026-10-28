import { Alert, Collapse, Tag, Typography } from 'antd'
import { ModelCalls } from './ModelDiagnostics'
import { errorTypeLabel, runDiagnosticSummary } from './diagnosticSummary'
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
  const summary = runDiagnosticSummary(run)
  const calls = run.final_state?.protocol_calls
  const agents = Object.entries(run.metrics.agent_usage ?? {})
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
    <section aria-label="运行诊断">
      <h3>运行诊断</h3>
      {run.error && <Alert type="error" showIcon title={errorTypeLabel(run.error.error_type)} description={run.error.message} />}
      {run.status === 'completed' && summary.hasIssues && <Alert type="warning" showIcon title={summary.heading}
        description="本次流程最终完成，以下保留中途发生的异常调用和重试记录。完成不代表答案正确。" />}
      {!summary.hasIssues && <p className="diagnostic-note">未记录请求或结构化输出异常。答案质量请结合评分与证据判断。</p>}
      {!!summary.labels.length && <p className="diagnostic-labels">{summary.labels.map(label => <Tag key={label}>{label}</Tag>)}</p>}
      {agents.filter(([, usage]) => !!usage.calls?.length).map(([agentId, usage]) => {
        const stages = [...new Set(Object.values(calls ?? {}).filter(call => call.agent_id === agentId).map(call => call.stage_id))]
        const name = Object.values(calls ?? {}).find(call => call.agent_id === agentId)?.agent_name
        return <section className="evaluation-agent-calls" key={agentId} aria-label={`${agentId} 模型调用`}>
          <h4>{name ? `${name} · ` : ''}{agentId}</h4>
          <p className="diagnostic-note">参与阶段：{stages.length ? stages.join('、') : '未记录'}；下列按该智能体的请求顺序展示。</p>
          <ModelCalls calls={usage.calls} />
        </section>
      })}
      {!agents.some(([, usage]) => !!usage.calls?.length) && summary.hasIssues &&
        <p className="diagnostic-note">没有保存逐次模型调用详情。缺失的 HTTP 状态码、供应商错误码、Request ID 和重试信息无法从历史记录恢复。</p>}
    </section>
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
