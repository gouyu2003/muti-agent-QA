import { Collapse, Empty, Table, Tag } from 'antd'
import type { EvaluationJob, MethodSummary } from './evaluationApi'
import { targetLabel } from './evaluationLabels'

export default function EvaluationSummary({ job }: { job: EvaluationJob }) {
  if (!job.report) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="实验报告将在执行后生成" />
  const rows = Object.entries(job.report.methods).map(([method, data]) => ({ method, ...data }))
  const seconds = (value?: number | null) => value == null ? '未知' : (value / 1000).toFixed(1)
  return <section className="evaluation-summary" aria-label="实验结果概览">
    <div className="evaluation-section-heading"><h3>方法对比</h3><Tag>首次尝试 · 主结果</Tag></div>
    <Table<MethodSummary & { method: string }> size="small" pagination={false} scroll={{ x: 640 }} rowKey="method" dataSource={rows}
      columns={[
        { title: '对比对象', dataIndex: 'method', render: method => targetLabel(job, method) },
        ...['em', 'f1', 'sp_f1', 'joint_f1'].map(metric => ({
          title: metric.toUpperCase(), key: metric, align: 'right' as const,
          render: (_: unknown, row: MethodSummary) => {
            const score = row.scores[metric]
            return score?.value == null ? '—' : <span className="evaluation-score">
              {(score.value * 100).toFixed(1)}% <small>({score.scored})</small>
            </span>
          },
        })),
        { title: '平均耗时 / 秒', align: 'right', render: (_, row) => seconds(row.usage.duration_ms?.mean_reported) },
      ]} />
    <p className="evaluation-hint">括号为各指标的已评分题数。主成绩按首次尝试统计，失败计零，无标准答案的指标显示为未知。</p>
    {!job.report.complete && <p className="evaluation-hint evaluation-warning">首次尝试尚未完整，完整集成绩将在首次尝试全部结束后显示。</p>}
    <div className="evaluation-section-heading"><h3>模型用量</h3><small>已知用量</small></div>
    <Table size="small" pagination={false} rowKey="method" dataSource={rows} scroll={{ x: 500 }} columns={[
      { title: '对比对象', dataIndex: 'method', render: method => targetLabel(job, method) },
      { title: 'Token 合计', align: 'right', render: (_, row) => row.usage.total_tokens?.reported_total?.toLocaleString() ?? '未知' },
      { title: '模型调用', align: 'right', render: (_, row) => row.usage.llm_call_count?.reported_total ?? '未知' },
      { title: '完成 / 失败', align: 'right', render: (_, row) => `${row.statuses.completed ?? 0} / ${row.statuses.failed ?? 0}` },
    ]} />
    <Collapse ghost items={[{ key: 'costs', label: '查看开销分布与评分说明', children: <>
      <Table size="small" pagination={false} rowKey="method" dataSource={rows} scroll={{ x: 620 }} columns={[
        { title: '对比对象', dataIndex: 'method', render: method => targetLabel(job, method) },
        { title: '耗时中位数 / 秒', align: 'right', render: (_, row) => seconds(row.usage.duration_ms?.median_reported) },
        { title: '耗时 P95 / 秒', align: 'right', render: (_, row) => seconds(row.usage.duration_ms?.p95_reported) },
        { title: '已知输入 / 输出 Token', align: 'right', render: (_, row) => `${row.usage.prompt_tokens?.reported_total ?? '未知'} / ${row.usage.completion_tokens?.reported_total ?? '未知'}` },
      ]} />
      <p className="evaluation-hint">EM/F1 沿用英文 HotpotQA 归一化规则。执行完成包含回答错误的情况，已知 Token 合计不包含未知用量。</p>
    </> }]} />
  </section>
}
