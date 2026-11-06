import { Alert, Collapse, Table, Tag, Typography } from 'antd'
import { ModelCalls } from './ModelDiagnostics'
import { errorTypeLabel, runDiagnosticSummary } from './diagnosticSummary'
import type { ModelCall, RunActivity, RunTimings } from './types'

export const ACTIVITY: Record<string, string> = {
  configuration_load: '加载运行配置', knowledge_load: '加载知识源', retrieval: '检索证据',
  model_request: '等待模型响应', retry_wait: '请求重试等待', output_repair: '输出修正',
  initial_output: '初次输出', output_validation: '校验输出', result_save: '保存结果', execution: '执行流程',
  node: '执行智能体任务',
}

export function groupDiagnosticCalls(calls: ModelCall[] = []) {
  const groups: Record<string, { calls: ModelCall[] }> = {}
  for (const call of calls) (groups[call.agent_id ?? 'runtime'] ??= { calls: [] }).calls.push(call)
  return groups
}

export function TimingDetails({ timings }: { timings?: RunTimings }) {
  if (!timings?.categories || !Object.keys(timings.categories).length) return <p className="diagnostic-note">耗时拆分未记录。</p>
  return <Collapse items={[{ key: 'timing', label: '耗时分析', children: <>
    {timings.task_total_wall_ms != null && <p>任务总耗时：{(timings.task_total_wall_ms/1000).toFixed(3)} 秒（加载至执行与保存结束的观测跨度）</p>}
    {!!timings.cache?.length && <p>知识源缓存：命中 {timings.cache.filter(item => item.cache_hit).length} 次／读取 {timings.cache.length} 次</p>}
    <Table size="small" pagination={false} rowKey="name" dataSource={Object.entries(timings.categories).map(([name, data]) => ({ name, ...data }))}
      columns={[{ title: '环节', dataIndex: 'name', render: name => ACTIVITY[name] ?? name },
        { title: '实际经过 / 秒', dataIndex: 'wall_ms', render: value => (value / 1000).toFixed(3) },
        { title: '累计工作 / 秒', dataIndex: 'cumulative_ms', render: value => (value / 1000).toFixed(3) },
        { title: '次数', dataIndex: 'count' }]} />
    <p className="diagnostic-note">同类并行区间合并后为实际经过时间。不同环节可能嵌套，不能相加；执行耗时保留原统计口径。</p>
    {!!timings.spans?.length && <details><summary>阶段与请求区间</summary>
      <Table size="small" pagination={{ pageSize: 10 }} rowKey="key" scroll={{ x: 500 }}
        dataSource={timings.spans.map((span, key) => ({ key, ...span }))} columns={[
          { title: '环节', dataIndex: 'name', render: name => ACTIVITY[name] ?? name },
          { title: '阶段 / 智能体', render: (_, span) => [span.stage_id, span.agent_id].filter(Boolean).join(' / ') || '未记录' },
          { title: '经过 / 秒', dataIndex: 'duration_ms', render: value => (value/1000).toFixed(3) },
          { title: '关联请求', render: (_, span) => span.request_call_id ? <Typography.Text copyable={{ text: span.request_call_id }}>{span.request_call_id}</Typography.Text> : '—' },
        ]} />
    </details>}
  </> }]} />
}

export function ActivityDetails({ activity, connection }: { activity?: RunActivity; connection?: string }) {
  if (!activity && !connection) return null
  const live = activity && ['queued', 'running', 'cancelling'].includes(activity.status)
  return <section className="run-activity" aria-label="后台运行状态" aria-live="polite">
    <p><strong>{connection === 'reconnecting' ? '连接中断，正在重新连接' : '已连接运行服务'}</strong>
      {activity && <> · 后台：{({ queued: '准备启动', running: '运行中', cancelling: '正在停止', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '执行中断' } as Record<string, string>)[activity.status] ?? activity.status}</>}</p>
    {activity && <Typography.Text copyable={{ text: activity.run_id }}>运行 ID：{activity.run_id}</Typography.Text>}
    {live && <p className="diagnostic-note">后台运行与页面连接独立，刷新或关闭页面后继续执行。</p>}
    {activity?.heartbeat_stale && <Alert type="warning" title="存活信息暂未更新，正在核对执行进程。" />}
    {live && activity?.activities?.filter(item => !['execution', 'initial_output'].includes(item.name)).map(item => <p key={item.span_id}>
      {ACTIVITY[item.name] ?? item.name}{item.stage_id ? ` · ${item.stage_id}` : ''}{item.agent_id ? ` · ${item.agent_id}` : ''}
      {' · '}已等待 {Math.max(0, (Date.now()-Date.parse(item.timestamp))/1000).toFixed(0)} 秒
    </p>)}
    {activity?.last_progress_at && <p className="diagnostic-note">最后进展：{new Date(activity.last_progress_at).toLocaleString('zh-CN', { hour12: false })}</p>}
  </section>
}

export function RunDiagnostics({ run }: { run: {
  status: string; error?: { error_type: string; message: string } | null
  metrics?: { agent_usage?: Record<string, { calls?: ModelCall[] }> }; labels?: string[]
} }) {
  const summary = runDiagnosticSummary(run)
  return <section aria-label="运行诊断">
    <h3>运行诊断</h3>
    {run.error && <Alert showIcon type="error" title={errorTypeLabel(run.error.error_type)} description={run.error.message} />}
    <p><strong>{summary.heading}</strong>{run.status === 'completed' && summary.hasIssues ? '；最终流程完成，中途异常仍保留。' : ''}</p>
    {!summary.hasIssues && <p className="diagnostic-note">未记录请求或结构化输出异常。答案质量请结合评分与证据判断。</p>}
    <p>{summary.labels.map(label => <Tag key={label}>{label}</Tag>)}</p>
    <Collapse defaultActiveKey={summary.hasIssues ? Object.keys(run.metrics?.agent_usage ?? {}) : []} items={Object.entries(run.metrics?.agent_usage ?? {}).filter(([, usage]) => usage.calls?.length).map(([id, usage]) => ({
      key: id, label: `${id} · 模型请求与校验详情`, children: <ModelCalls calls={usage.calls} />,
    }))} />
    {summary.hasIssues && !Object.values(run.metrics?.agent_usage ?? {}).some(usage => usage.calls?.length) &&
      <p className="diagnostic-note">逐次请求详情未记录，历史缺失信息无法恢复。</p>}
  </section>
}
