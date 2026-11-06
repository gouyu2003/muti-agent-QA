import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Empty, Input, Select } from 'antd'
import { searchKnowledge } from './api'
import type { SearchResults } from './api'

export default function KnowledgeSearch({ sourceId, version }: { sourceId: string; version: number }) {
  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState(5)
  const [result, setResult] = useState<SearchResults>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const controller = useRef<AbortController | undefined>(undefined)
  useEffect(() => () => controller.current?.abort(), [])
  async function search() {
    controller.current?.abort()
    const request = new AbortController()
    controller.current = request
    setBusy(true); setError(''); setResult(undefined)
    try { setResult(await searchKnowledge(sourceId, version, query.trim(), topK, request.signal)) }
    catch (reason) { if (!request.signal.aborted) setError(reason instanceof Error ? reason.message : '检索失败，请重试。') }
    finally { if (!request.signal.aborted) setBusy(false) }
  }
  return <section className="knowledge-search" aria-label="检索试查">
    <h3>检索试查</h3>
    <p className="diagnostic-note">只检索当前材料版本，不调用模型。相关性分数用于排序，不表示答案正确的概率。</p>
    <form onSubmit={event => { event.preventDefault(); if (query.trim() && !busy) void search() }}>
      <Input aria-label="检索问题" placeholder="输入中文、英文或混合问题" maxLength={2000} value={query} onChange={event => setQuery(event.target.value)} />
      <Select aria-label="返回片段数" value={topK} onChange={setTopK} options={[3, 5, 10, 20].map(value => ({ value, label: `前 ${value} 条` }))} />
      <Button htmlType="submit" type="primary" aria-label="检索" loading={busy} disabled={!query.trim()}>检索</Button>
    </form>
    {error && <Alert type="error" title="检索失败" description={error} />}
    {result && <>
      <p>版本 {result.version} · 索引 {result.index_version} · 命中 {result.items.length} 条</p>
      {!result.items.length && <Empty description="没有匹配片段，可调整问题中的关键词。" />}
      {result.items.map((item, index) => <article className="knowledge-chunk" key={item.chunk_id ?? index}>
        <strong>#{index+1} · {item.filename ?? item.source_title}{item.line_start != null ? ` · 第 ${item.line_start}–${item.line_end} 行` : ''}</strong>
        <small>相关性 {item.relevance_score.toPrecision(4)}</small><p>{item.text}</p>
      </article>)}
    </>}
  </section>
}
