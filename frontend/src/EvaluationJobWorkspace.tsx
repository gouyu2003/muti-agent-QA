import { useState } from 'react'
import { Alert, Button, Checkbox, Collapse, Dropdown, Empty, Modal, Progress, Tabs, Tag, Tooltip } from 'antd'
import { MoreHorizontal } from 'lucide-react'
import { evaluationRequest, type EvaluationJob, type EvaluationResult } from './evaluationApi'
import { targetLabel } from './evaluationLabels'
import { CopyOutlined, DownloadOutlined } from './Icons'
import EvaluationSummary from './EvaluationSummary'
import EvaluationReview from './EvaluationReview'
import EvaluationResearch from './EvaluationResearch'

export const EVALUATION_STATUS: Record<string, string> = {
  draft: '待启动', queued: '排队中', running: '运行中', stopping: '等待当前题结束',
  paused: '已暂停', completed: '执行结束', interrupted: '执行中断', error: '执行异常',
}
export const evaluationActive = (status: string) => ['queued', 'running', 'stopping'].includes(status)
export const EVALUATION_PURPOSE: Record<string, string> = { smoke: '试跑', development: '开发实验', evaluation: '正式评价' }

interface Props {
  job: EvaluationJob
  busy: boolean
  perform: (fn: () => Promise<void>) => Promise<boolean>
  onUpdate: (job: EvaluationJob) => void
  onCopy: () => void
  onArchive: () => void
  onOpenDetail: (row: EvaluationResult) => void
}

export default function EvaluationJobWorkspace({ job, busy, perform, onUpdate, onCopy, onArchive, onOpenDetail }: Props) {
  const [tab, setTab] = useState('overview')
  const [runAction, setRunAction] = useState<'start' | 'retry'>()
  const [consent, setConsent] = useState(false)
  const [runError, setRunError] = useState('')
  const active = evaluationActive(job.status)
  const retryCount = job.retryable_count ?? 0
  const retryDisabledReason = job.archived ? '取消归档后可以继续或补跑。'
    : active ? '当前任务正在运行，请等待或暂停后再补跑。'
      : retryCount === 0 ? job.progress.attempted === job.progress.planned
        ? '暂无可补跑项，所有任务均已完成。' : '暂无可补跑项；尚未开始的任务可通过“继续未开始的任务”执行。'
        : busy ? '正在处理，请稍候。' : undefined
  const finished = job.progress.completed + job.progress.failed + job.progress.interrupted + (job.progress.cancelled ?? 0)
  const progress = job.progress.planned ? Math.round(finished / job.progress.planned * 100) : 0
  const startDisabled = busy || !consent || active || !!job.archived || (runAction === 'retry' && retryCount === 0)
  const base = `/api/v1/evaluations/jobs/${job.job_id}`
  const exports = [
    { key: 'csv', label: <a href={`${base}/export?format=csv`}>逐题 CSV / 人工审阅表</a> },
    { key: 'json', label: <a href={`${base}/export?format=json`}>报告 JSON</a> },
    ...(job.report?.research ? [
      { key: 'zip', label: <a href={`${base}/report?format=zip`}>完整实验报告包 ZIP</a> },
      { key: 'markdown', label: <a href={`${base}/report?format=markdown`}>研究报告 Markdown</a> },
      { key: 'pairs', label: <a href={`${base}/report?format=pairs_csv`}>配对差值 CSV</a> },
    ] : []),
  ]
  function closeRun() { setRunAction(undefined); setConsent(false); setRunError('') }
  function openRun(action: 'start' | 'retry') { setRunAction(action); setConsent(false); setRunError('') }
  async function start() {
    if (startDisabled || !runAction) return
    const success = await perform(async () => {
      try {
        const result = await evaluationRequest<EvaluationJob>(`/jobs/${job.job_id}/start`, 'POST', {
          allow_model_calls: true, ...(runAction === 'retry' ? { retry_failed: true } : {}),
        })
        onUpdate(result)
      } catch (error) {
        setRunError(error instanceof Error ? error.message : '启动失败')
        throw error
      }
    })
    if (success) { closeRun(); setTab('overview') }
  }
  return <article className="evaluation-job-detail evaluation-panel">
    <header className="evaluation-detail-header">
      <div className="evaluation-detail-title"><h2>{job.name}</h2><Tag color={job.status === 'completed' ? 'success' : active ? 'processing' : 'default'}>{EVALUATION_STATUS[job.status] ?? job.status}</Tag></div>
      <div className="evaluation-meta">
        <span>{EVALUATION_PURPOSE[String(job.plan?.request.purpose)] ?? '历史未标用途'}</span>
        <span>{job.sample_count} 题 × {job.targets.length} 个对象 × {job.repeats} 次</span>
        <span>{job.context_mode === 'question_context' ? '每题 context' : '系统固定知识源'}</span>
      </div>
      <div className="evaluation-progress-heading"><span>{active ? '后台评测进行中' : finished === job.progress.planned ? '首次尝试已全部结束' : '首次尝试进度'}</span><small>{finished} / {job.progress.planned} 项</small></div>
      <Progress percent={progress} showInfo={false} size="small" status={active ? 'active' : 'normal'} strokeColor={job.status === 'completed' ? '#1f7551' : undefined} />
      <div className="evaluation-counts"><span>完成 <strong>{job.progress.completed}</strong></span><span>失败 <strong>{job.progress.failed}</strong></span><span>状态不明 <strong>{job.progress.interrupted}</strong></span>
        {!!job.progress.cancelled && <span>取消 <strong>{job.progress.cancelled}</strong></span>}</div>
      <div className="evaluation-detail-actions">
        {!job.archived && !active && (job.status === 'draft' || job.progress.attempted < job.progress.planned) &&
          <Button type="primary" disabled={busy} onClick={() => openRun('start')}>{job.status === 'draft' ? '启动评测' : '继续未开始的任务'}</Button>}
        {active && <Button disabled={busy || job.status === 'stopping'} onClick={() => void perform(async () => onUpdate(await evaluationRequest<EvaluationJob>(`/jobs/${job.job_id}/stop`, 'POST')))}>暂停评测</Button>}
        <Dropdown menu={{ items: exports }} trigger={['click']}><Button icon={<DownloadOutlined />}>导出报告</Button></Dropdown>
        <Button type="text" icon={<CopyOutlined />} disabled={busy} onClick={onCopy}>复制为新实验配置</Button>
        {job.status !== 'draft' && <Tooltip title={retryDisabledReason} trigger={['hover', 'focus']}>
          <span tabIndex={retryDisabledReason ? 0 : undefined}><Button type="text" disabled={!!retryDisabledReason} onClick={() => openRun('retry')}>补跑失败／状态不明项{retryCount > 0 ? `（${retryCount}）` : ''}</Button></span>
        </Tooltip>}
        <Dropdown trigger={['click']} menu={{ items: [{ key: 'archive', label: job.archived ? '取消归档' : '归档任务（保留结果）', disabled: busy || active, onClick: onArchive }] }}>
          <Button type="text" aria-label="更多实验操作" icon={<MoreHorizontal size={17} aria-hidden="true" />} />
        </Dropdown>
      </div>
      {retryCount === 0 && job.status !== 'draft' && !active && !job.archived && <p className="evaluation-hint">{retryDisabledReason}</p>}
      {job.error && <Alert type="error" showIcon title={job.error} />}
      {job.archived && <Alert type="info" showIcon title="此任务已归档，可通过“更多实验操作”取消归档。" />}
    </header>
    <Tabs className="evaluation-result-tabs" activeKey={tab} onChange={setTab} items={[
      { key: 'overview', label: '结果概览', children: <>
        {active && <EvaluationReview mode="monitor" enabled={tab === 'overview'} job={job} base={`/jobs/${job.job_id}`} onOpenDetail={onOpenDetail} />}
        <EvaluationSummary job={job} />
        {(job.report?.retry_count ?? 0) > 0 && <p className="evaluation-hint">已有补跑记录，主成绩保留首次尝试。<Button type="link" size="small" onClick={() => setTab('questions')}>查看补跑补充统计</Button></p>}
      </> },
      { key: 'questions', label: '逐题结果', children: <EvaluationReview mode="results" enabled={tab === 'questions'} job={job} base={`/jobs/${job.job_id}`} onOpenDetail={onOpenDetail} /> },
      { key: 'research', label: '研究统计', children: job.report?.research ? <EvaluationResearch job={job} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前任务尚无研究统计报告" /> },
      { key: 'plan', label: '冻结方案', children: <>
        <div className="evaluation-section-heading"><h3>实验条件</h3><Tag>已固定</Tag></div>
        <dl className="evaluation-definition"><div><dt>问题库</dt><dd>{job.bank.name} · v{job.bank.version}</dd></div><div><dt>对比对象</dt><dd>{job.targets.map(target => targetLabel(job, target.method_id)).join('、')}</dd></div>
          <div><dt>材料策略</dt><dd>{job.context_mode === 'question_context' ? '使用每道题自带的 context' : '沿用系统固定知识源'}</dd></div><div><dt>冻结指纹</dt><dd className="evaluation-fingerprint">{job.fingerprint}</dd></div></dl>
        {job.plan && <Collapse items={[{ key: 'frozen', label: '查看本任务冻结方案与题目清单', children: <pre className="evaluation-json">{JSON.stringify(job.plan, null, 2)}</pre> }]} />}
        <p className="evaluation-hint">后台执行与页面独立，关闭页面后任务继续。重启电脑会中断，恢复时核对终态。补跑另存尝试，主表保留首次成绩。</p>
      </> },
    ]} />
    <Modal className="evaluation-console-modal" title={runAction === 'retry' ? '确认补跑' : '确认启动评测'} open={!!runAction} onCancel={() => !busy && closeRun()}
      footer={<div className="evaluation-button-row"><Button disabled={busy} onClick={closeRun}>取消</Button><Button type="primary" loading={busy} disabled={startDisabled} onClick={() => void start()}>{runAction === 'retry' ? '确认补跑' : '启动后台评测'}</Button></div>}>
      {runError && <Alert type="error" showIcon title={runError} />}
      <p>{job.name}</p><p className="evaluation-hint">{runAction === 'retry' ? `本次补跑 ${retryCount} 个失败、取消或状态不明项。` : `本次将执行尚未开始的问答，原计划共 ${job.progress.planned} 项。`}实际模型调用和费用取决于协作过程。</p>
      <Checkbox checked={consent} disabled={busy} onChange={event => setConsent(event.target.checked)}>我确认启动会调用真实模型并产生费用</Checkbox>
    </Modal>
  </article>
}
