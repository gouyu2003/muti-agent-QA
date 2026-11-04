import { useState } from 'react'
import { Alert, Collapse, Select, Space, Table } from 'antd'
import type { EvaluationJob } from './evaluationApi'
import { targetLabel } from './evaluationLabels'

export default function EvaluationResearch({ job }: { job: EvaluationJob }) {
  const research = job.report?.research
  const [metric, setMetric] = useState<string>()
  const [grouping, setGrouping] = useState<'by_type' | 'by_repeat'>('by_type')
  const [pairScope, setPairScope] = useState<'reference' | 'all'>('reference')
  if (!research) return null
  const selectedMetric = metric ?? research.primary_metric
  const scope = research.reference_pairs ? pairScope : 'all'
  const label = (key: string) => targetLabel(job, key)
  const format = (value?: number | null) => value == null ? '不适用／数据不完整' : `${(value * 100).toFixed(2)}%`
  const grouped = Object.entries(research.methods).flatMap(([method, data]) => Object.entries(data[grouping]).map(([group, values]) => ({
    key: `${method}:${group}`, method: label(method), group,
    value: values.scores[selectedMetric]?.value, completed: values.statuses.completed, failed: values.statuses.failed,
    std: data.repeat_score_std[selectedMetric],
  })))
  return <section aria-label="研究统计报告">
    <h3>研究统计报告</h3>
    {!research.complete && <Alert type="info" title="首次尝试尚未全部结束；配对区间将在完整结果产生后生成。" />}
    <p>主指标：{research.primary_metric.toUpperCase()}；比较基准：{label(research.reference_method)}；按题 bootstrap {research.bootstrap.replicates} 次，95% 区间。</p>
    <Select aria-label="研究统计指标" value={selectedMetric} onChange={setMetric}
      options={['em', 'f1', 'sp_em', 'sp_f1', 'joint_em', 'joint_f1'].map(value => ({ value, label: value.toUpperCase() }))} />
    <Select aria-label="配对比较范围" value={scope} onChange={setPairScope} options={[
      { value: 'reference', label: '主要比较：相对指定基准', disabled: !research.reference_pairs }, { value: 'all', label: '全部两两比较（含次要／探索性）' },
    ]} />
    <Collapse defaultActiveKey={['pairs']} items={[{ key: 'pairs', label: '同题配对比较（差值为右侧减左侧）', children:
      <Table size="small" rowKey={r => `${r.left}:${r.right}`} pagination={{ pageSize: 10 }} scroll={{ x: 750 }} dataSource={scope === 'reference' ? research.reference_pairs : research.pairs}
        columns={[{ title: '左侧', dataIndex: 'left', render: label }, { title: '右侧', dataIndex: 'right', render: label },
          { title: '配对题数', dataIndex: 'paired_samples' },
          { title: '差值', key: 'delta', render: (_, r) => format(r.metrics[selectedMetric]?.delta) },
          { title: '95% 区间', key: 'ci', render: (_, r) => r.metrics[selectedMetric]?.ci95_sample_bootstrap.map(format).join(' ～ ') ?? '不适用／数据不完整' }]} /> },
      { key: 'groups', label: '题型与重复实验分层', children: <>
        <Select aria-label="研究分层方式" value={grouping} onChange={setGrouping} options={[{ value: 'by_type', label: '按题型' }, { value: 'by_repeat', label: '按重复次数' }]} />
        <Table size="small" rowKey="key" dataSource={grouped} pagination={{ pageSize: 10 }} scroll={{ x: 650 }} columns={[
          { title: '对象', dataIndex: 'method' }, { title: '分组', dataIndex: 'group' }, { title: '完成', dataIndex: 'completed' },
          { title: '失败', dataIndex: 'failed' }, { title: '得分', dataIndex: 'value', render: format },
          { title: '整体跨重复得分标准差', dataIndex: 'std', render: v => v == null ? '重复不足或结果不完整' : format(v) },
        ]} />
      </> }]} />
    {research.notes.map(note => <p key={note}><small>{note}</small></p>)}
    {!job.readonly && <Space wrap>
      <a href={`/api/v1/evaluations/jobs/${job.job_id}/report?format=markdown`}>导出研究报告 Markdown</a>
      <a href={`/api/v1/evaluations/jobs/${job.job_id}/report?format=pairs_csv`}>导出配对差值 CSV</a>
      <a href={`/api/v1/evaluations/jobs/${job.job_id}/report?format=json`}>导出研究统计 JSON</a>
      <a href={`/api/v1/evaluations/jobs/${job.job_id}/report?format=zip`}>下载完整实验报告包</a>
    </Space>}
  </section>
}
