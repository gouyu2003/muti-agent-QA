import type { EvidenceLocation as Location } from './types'

export default function EvidenceLocation({ evidence }: { evidence: Location & { source_id?: string; source_paragraph?: number } }) {
  if (!evidence.filename) return evidence.source_paragraph !== undefined ? <small>句子索引 {evidence.source_paragraph}</small> : null
  return (
    <small className="evidence-location">
      {evidence.source_id} · 版本 {evidence.source_version} · {evidence.filename}
      {' · '}第 {evidence.line_start}–{evidence.line_end} 行
      {evidence.heading_path?.length ? ` · ${evidence.heading_path.join(' / ')}` : ''}
    </small>
  )
}
