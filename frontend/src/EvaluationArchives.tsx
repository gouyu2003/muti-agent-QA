import { useEffect, useState } from 'react'
import { Alert, Button, Collapse, Select, Space, Table } from 'antd'
import { evaluationRequest, type EvaluationArchive, type EvaluationResult } from './evaluationApi'
import EvaluationResearch from './EvaluationResearch'
import EvaluationReview from './EvaluationReview'

interface ArchiveSummary { archive_id: string; name: string; imported_at: string; purpose: string; complete: boolean; fingerprint: string }
interface Candidate { key: string; name: string; available: boolean; reason: string | null; purpose: string | null; complete: boolean | null }
export default function EvaluationArchives({ onOpenDetail }: { onOpenDetail: (row: EvaluationResult, base: string) => void }) {
  const [items, setItems] = useState<ArchiveSummary[]>([])
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [source, setSource] = useState<string>()
  const [archive, setArchive] = useState<EvaluationArchive>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function refresh() {
    const [list, available] = await Promise.all([
      evaluationRequest<{ items: ArchiveSummary[] }>('/archives'),
      evaluationRequest<{ items: Candidate[] }>('/archives/candidates'),
    ])
    setItems(list.items ?? []); setCandidates(available.items ?? [])
  }
  useEffect(() => { void refresh().catch(e => setError(String(e))) }, [])
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError('')
    try { await fn() } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  return <section aria-label="旧 CLI 只读实验档案">
    <Alert type="info" title="导入保存的是历史结果只读快照；不能从这里启动、补跑或改写原实验。逐题结果和报告独立保存，原轨迹需保留源目录。" />
    {error && <Alert type="error" title={error} />}
    <Space wrap>
      <Select aria-label="CLI 来源档案" value={source} placeholder="选择原实验目录中的档案" onChange={setSource}
        options={candidates.map(c => ({ value: c.key, label: `${c.name}${c.available ? '' : '（暂不可导入）'}`, disabled: !c.available, title: c.reason ?? undefined }))} />
      <Button disabled={!source || busy} onClick={() => void action(async () => {
        const result = await evaluationRequest<EvaluationArchive>('/archives', 'POST', { key: source })
        setArchive(result); await refresh()
      })}>导入只读快照</Button>
      <Button disabled={busy} onClick={() => void action(refresh)}>刷新 CLI 档案列表</Button>
    </Space>
    {candidates.filter(c => !c.available).map(c => <p key={c.key}><small>{c.name}：{c.reason}</small></p>)}
    <Table size="small" rowKey="archive_id" dataSource={items} pagination={{ pageSize: 5 }} scroll={{ x: 650 }} columns={[
      { title: '历史实验', dataIndex: 'name' }, { title: '用途', dataIndex: 'purpose' },
      { title: '原报告完整', dataIndex: 'complete', render: v => v ? '是' : '否' },
      { title: '登记时间', dataIndex: 'imported_at', render: v => new Date(v).toLocaleString() },
      { title: '查看', render: (_, r) => <Button disabled={busy} onClick={() => void action(async () => setArchive(await evaluationRequest<EvaluationArchive>(`/archives/${r.archive_id}`)))}>查看历史结果</Button> },
    ]} />
    {archive && <>
      <h3>{archive.view.name} · 只读</h3>
      <Button onClick={() => setArchive(undefined)}>关闭历史结果</Button>
      <p>快照指纹：{archive.fingerprint}</p>
      <p>以下保留原报告的首次尝试成绩与分母，不使用当前代码重新评分。</p>
      <Table size="small" rowKey="method" pagination={false} scroll={{ x: 700 }}
        dataSource={Object.entries(archive.view.report?.methods ?? {}).map(([method, data]) => ({ method, ...data }))} columns={[
          { title: '对象', dataIndex: 'method' }, { title: '完成／失败', render: (_, r) => `${r.statuses.completed} / ${r.statuses.failed}` },
          { title: 'EM', render: (_, r) => r.scores.em?.value == null ? '未知' : `${(r.scores.em.value * 100).toFixed(1)}%` },
          { title: 'F1', render: (_, r) => r.scores.f1?.value == null ? '未知' : `${(r.scores.f1.value * 100).toFixed(1)}%` },
          { title: '已知 Token', render: (_, r) => r.usage.total_tokens?.reported_total ?? '未知' },
          { title: '模型调用', render: (_, r) => r.usage.llm_call_count?.reported_total ?? '未知' },
        ]} />
      <Collapse items={[{ key: 'provenance', label: '原实验配置与来源记录', children: <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify({ config: archive.config, provenance: archive.provenance }, null, 2)}</pre> }]} />
      <EvaluationResearch key={archive.archive_id} job={archive.view} />
      <Space wrap><a href={`/api/v1/evaluations/archives/${archive.archive_id}/export?format=json`}>导出历史报告与结果 JSON</a>
        <a href={`/api/v1/evaluations/archives/${archive.archive_id}/export?format=csv`}>导出历史逐题 CSV</a></Space>
      <EvaluationReview key={archive.archive_id} job={archive.view} base={`/archives/${archive.archive_id}`}
        onOpenDetail={row => onOpenDetail(row, `/archives/${archive.archive_id}`)} />
    </>}
  </section>
}
