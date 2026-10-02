import type { EvidenceLocation as Location, SourceOption } from './types'

export default function EvidenceLocation({ evidence, source }: {
  evidence: Location & { source_id?: string; source_paragraph?: number }
  source?: Pick<SourceOption, 'source_id' | 'name'>
}) {
  if (!evidence.filename) return evidence.source_paragraph !== undefined ? <small>句子索引 {evidence.source_paragraph}</small> : null
  const sourceName = source?.source_id === evidence.source_id ? source?.name.trim() : undefined
  return (
    <small className="evidence-location">
      {evidence.source_id ? <><span title={`知识源 ID：${evidence.source_id}`}>{sourceName || evidence.source_id}</span>{' · '}</> : null}
      版本 {evidence.source_version} · {evidence.filename}
      {' · '}第 {evidence.line_start}–{evidence.line_end} 行
      {evidence.heading_path?.length ? ` · ${evidence.heading_path.join(' / ')}` : ''}
    </small>
  )
}
