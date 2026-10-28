import { Collapse, Tag } from 'antd'
import EvidenceLocation from './EvidenceLocation'
import ModelDiagnostics, { RequestErrorDetails } from './ModelDiagnostics'
import type { ProtocolCall } from './protocolTypes'

export default function ProtocolCalls({ calls }: { calls?: Record<string, ProtocolCall> }) {
  if (!calls || !Object.keys(calls).length) return null
  return <section aria-label="协作调用记录"><h3>协作调用记录</h3><Collapse items={Object.values(calls).sort((a, b) => a.invocation_index - b.invocation_index).map(c => ({
    key: c.call_id, label: `${c.stage_id} · 第 ${c.round} 轮 · ${c.agent_name} · ${c.kind}`,
    children: <div style={{ overflowWrap: 'anywhere' }}>
      {c.status === 'failed' && <Tag color="error">调用失败 · {c.output.error}</Tag>}
      {c.output.request_error && <RequestErrorDetails error={c.output.request_error} />}
      <p>实例：{c.agent_id} · 阶段：{c.stage_id} · 任务：{c.task_id}</p><small>调用：{c.call_id}</small>
      {c.input.task.instruction && <p>任务：{c.input.task.instruction}</p>}
      {c.output.action && <p>下一步：{c.output.action === 'dispatch' ? '继续分派' : '提交审核'} · {c.output.reason}</p>}
      {c.output.tasks?.map(t => <p key={t.task_id}>{t.task_id} → {t.agent_id}：{t.instruction}</p>)}
      {c.output.summary && <p>{c.output.summary}</p>}
      {c.output.answer && <p>观点：{c.output.answer}</p>}
      {c.output.critiques?.map(ch => <article key={ch.target_call_id}><Tag>{({ agree: '同意', challenge: '质疑', missing: '证据不足' })[ch.stance] ?? ch.stance}</Tag>
        <p>针对调用：{ch.target_call_id}</p><p>{ch.issue}</p>{ch.evidence_ids.length > 0 && <small>证据：{ch.evidence_ids.join('、')}</small>}
      </article>)}
      {c.output.responses?.map(r => <article key={r.critique_call_id}><p>回应质疑调用：{r.critique_call_id}</p><p>{r.accepted ? '接受' : '保留意见'}：{r.response}</p></article>)}
      {c.output.change_note && <p>修订说明：{c.output.change_note}</p>}
      {c.output.verification_note && <p>证据判断：{c.output.verification_note}</p>}
      {c.output.sub_questions?.map(s => <p key={s.id}>{s.id}：{s.question}</p>)}
      {(c.output.supporting_evidence ?? []).map((e, i) => <div key={i}><EvidenceLocation evidence={e} /><p>{e.text}</p></div>)}
      {!c.output.supporting_evidence?.length && c.output.evidence_chunks?.map(g => <div key={g.sq_id}>{g.chunks.map((e, i) => <div key={i}><EvidenceLocation evidence={e} /><p>{e.text}</p></div>)}</div>)}
      <p>模型调用 {c.usage.call_count} 次 · Token {c.usage.total_tokens ?? '未知'}</p><ModelDiagnostics diagnostics={c.output.model_diagnostics} />
    </div>,
  }))} /></section>
}
