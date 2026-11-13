import { Tag } from 'antd'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import EvidenceLocation from './EvidenceLocation'
import type { RunResult } from './types'
import './answerContent.css'

export function answerStatus(result: RunResult, intent = result.chat_context?.intent) {
  if (intent === 'clarification') return { color: 'blue', label: '等待补充信息' }
  if (result.verification_applicable === false) return { color: 'default', label: '通用回答' }
  if (result.verified) return { color: 'success', label: '验证通过' }
  if (result.failure_type === 'conflict') return { color: 'warning', label: '资料存在冲突' }
  if (result.failure_type === 'insufficient') return { color: 'warning', label: '资料不足' }
  return { color: 'warning', label: '验证未通过' }
}

export default function AnswerContent({ result, content, intent, showStatus = true, showReferences = false }: {
  result: RunResult; content?: string; intent?: string; showStatus?: boolean; showReferences?: boolean
}) {
  const status = answerStatus(result, intent)
  const parts = result.response_parts?.length ? result.response_parts : undefined
  const mixed = (parts?.length ?? 0) > 1
  const references = showReferences && (result.supporting_evidence?.length ?? 0) > 0
    ? <details className="answer-references"><summary>查看引用 · {result.supporting_evidence!.length} 条</summary>
      {result.supporting_evidence!.map((evidence, index) => <div key={index} className="answer-reference-item">
        <strong>{evidence.source_title}</strong><EvidenceLocation evidence={evidence} /><p>{evidence.text}</p>
      </div>)}
    </details> : null
  return <div className="answer-content">
    {parts ? parts.map((part, index) => <section className="answer-part" key={`${part.kind}-${index}`} aria-label={part.kind === 'knowledge' ? '知识库部分' : '通用部分'}>
      {showStatus || mixed ? <div className="answer-part-heading">
        {mixed ? <strong>{part.kind === 'knowledge' ? '知识库部分' : '通用部分'}</strong> : null}
        {showStatus ? <Tag color={part.kind === 'general' ? 'default' : status.color}>{part.kind === 'general' ? '通用回答' : status.label}</Tag> : null}
      </div> : null}
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{part.content}</ReactMarkdown>
      {part.kind === 'knowledge' ? references : null}
    </section>) : <>
      {showStatus ? <Tag color={status.color}>{status.label}</Tag> : null}
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content ?? result.answer ?? ''}</ReactMarkdown>
      {result.verification_applicable !== false ? references : null}
    </>}
  </div>
}
