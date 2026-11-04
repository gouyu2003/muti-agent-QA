import { useEffect, useState } from 'react'
import { Alert, Button, Select, Space, Table, Tabs } from 'antd'
import { evaluationRequest, type EvaluationArchive, type EvaluationResult } from './evaluationApi'
import EvaluationResearch from './EvaluationResearch'
import EvaluationReview from './EvaluationReview'
import EvaluationSummary from './EvaluationSummary'

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
    <div className="evaluation-section-heading"><div><h2>旧 CLI 实验档案</h2><p>集中查阅已完成的历史实验，导入时保存报告和逐题结果的只读快照。</p></div></div>
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
      <div className="evaluation-section-heading"><h3>{archive.view.name} · 只读</h3><Button onClick={() => setArchive(undefined)}>关闭历史结果</Button></div>
      <p className="evaluation-hint">历史视图保留原报告的首次尝试成绩与分母，查看原轨迹需要保留源目录。</p>
      <Space wrap><a href={`/api/v1/evaluations/archives/${archive.archive_id}/export?format=json`}>导出历史报告与结果 JSON</a>
        <a href={`/api/v1/evaluations/archives/${archive.archive_id}/export?format=csv`}>导出历史逐题 CSV</a></Space>
      <Tabs key={archive.archive_id} items={[
        { key: 'overview', label: '结果概览', children: <EvaluationSummary job={archive.view} /> },
        { key: 'questions', label: '逐题结果', children: <EvaluationReview mode="results" job={archive.view} base={`/archives/${archive.archive_id}`} onOpenDetail={row => onOpenDetail(row, `/archives/${archive.archive_id}`)} /> },
        { key: 'research', label: '研究统计', children: <EvaluationResearch job={archive.view} /> },
        { key: 'source', label: '来源记录', children: <><p className="evaluation-fingerprint">快照指纹 {archive.fingerprint}</p><pre className="evaluation-json">{JSON.stringify({ config: archive.config, provenance: archive.provenance }, null, 2)}</pre></> },
      ]} />
    </>}
  </section>
}
