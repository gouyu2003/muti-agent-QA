import { Collapse, Tag } from 'antd'
import EvidenceLocation from './EvidenceLocation'
import ModelDiagnostics from './ModelDiagnostics'
import type { RunResult } from './types'

export default function AgentResults({ results }: { results?: RunResult['expert_results'] }) {
  if (!results || !Object.keys(results).length) return null
  return <section className="expert-results" aria-label="专家独立结果"><h3>专家独立结果</h3>
    <Collapse items={Object.entries(results).map(([id, result]) => ({ key: id,
      label: <span>{result.agent_name ?? id} · {result.answer || '未能作答'}</span>,
      children: <><Tag color={result.failure_type === 'output_error' ? 'error' : result.verified ? 'success' : 'warning'}>
        {result.failure_type === 'output_error' ? '模型输出错误' : result.failure_type === 'conflict' ? '证据存在冲突'
          : result.answer_scope === 'partial' && result.answer ? '提供部分信息，交由审核者汇总'
          : result.verified ? '内部验证通过' : '材料不足或答案尚未通过审核'}
      </Tag>
        <p>{result.verification_note}</p>
        {result.missing_information?.length ? <p>本实例材料未提供：{result.missing_information.join('；')}</p> : null}
        <ModelDiagnostics diagnostics={result.model_diagnostics} />
        {(result.supporting_evidence ?? []).map((e, i) => <div key={`${e.evidence_id ?? i}`}>
          <EvidenceLocation evidence={e} /><p>{e.text}</p>
        </div>)}
      </>,
    }))} />
  </section>
}
