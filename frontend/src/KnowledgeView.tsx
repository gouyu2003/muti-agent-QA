import { useEffect, useState } from 'react'
import { Alert, Button, Empty, Input, Modal, Pagination, Select, Skeleton, Tag } from 'antd'
import {
  getKnowledgeChunks, getKnowledgeSource, getKnowledgeSources,
  importKnowledge, deleteKnowledgeSource, KnowledgeImportError,
} from './api'
import type { ChunkPage, KnowledgeSource } from './types'

const STATUS = { importing: '处理中', ready: '可用', failed: '导入失败' }

export default function KnowledgeView({ disabled, onChanged, onUse }: {
  disabled: boolean
  onChanged: () => Promise<void>
  onUse: (sourceId: string, version: number) => void
}) {
  const [items, setItems] = useState<KnowledgeSource[]>([])
  const [detail, setDetail] = useState<KnowledgeSource>()
  const [version, setVersion] = useState<number>()
  const [name, setName] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [inputKey, setInputKey] = useState(0)
  const [targetId, setTargetId] = useState<string>()
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeSource>()
  const locked = busy || deleting || disabled
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [chunks, setChunks] = useState<ChunkPage>()
  const [chunksLoading, setChunksLoading] = useState(false)
  const [previewError, setPreviewError] = useState('')
  const [page, setPage] = useState(1)

  async function refresh(sourceId?: string) {
    setLoading(true)
    try {
      const listing = await getKnowledgeSources()
      setItems(listing.items)
      if (sourceId) {
        const selected = await getKnowledgeSource(sourceId)
        setDetail(selected)
        setVersion(selected.current_version ?? selected.versions[0]?.version)
        setPage(1)
      }
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    getKnowledgeSources(controller.signal).then((result) => setItems(result.items))
      .catch((reason: Error) => { if (reason.name !== 'AbortError') setError(reason.message) })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  const selectedVersion = detail?.versions.find((entry) => entry.version === version)
  const selectedReady = selectedVersion?.status === 'ready'
  useEffect(() => {
    setChunks(undefined)
    setPreviewError('')
    setChunksLoading(false)
    if (!detail || version === undefined || !selectedReady) return
    const controller = new AbortController()
    setChunksLoading(true)
    getKnowledgeChunks(detail.source_id, version, (page - 1) * 10, controller.signal)
      .then(result => { if (!controller.signal.aborted) setChunks(result) })
      .catch((reason: Error) => { if (reason.name !== 'AbortError') setPreviewError(reason.message) })
      .finally(() => { if (!controller.signal.aborted) setChunksLoading(false) })
    return () => controller.abort()
  }, [detail, version, page, selectedReady])

  async function handleImport() {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const imported = await importKnowledge(name.trim(), files, targetId)
      await refresh(imported.source_id)
      await onChanged()
      setNotice(`已导入「${imported.name}」版本 ${imported.current_version}。`)
      setFiles([])
      setInputKey((value) => value + 1)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导入失败')
      if (reason instanceof KnowledgeImportError && reason.sourceId) {
        await refresh(reason.sourceId).catch(() => {})
      }
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete() {
    if (!deleteTarget || locked) return
    const target = deleteTarget
    setDeleting(true); setError(''); setNotice('')
    try {
      await deleteKnowledgeSource(target.source_id)
      setItems(current => current.filter(item => item.source_id !== target.source_id))
      if (detail?.source_id === target.source_id) {
        setDetail(undefined); setVersion(undefined); setChunks(undefined); setPreviewError(''); setPage(1)
      }
      setTargetId(current => current === target.source_id ? undefined : current)
      setDeleteTarget(undefined)
      setNotice(`已删除「${target.name}」。已有系统快照和历史记录所需的材料仍保留。`)
      try { await onChanged() }
      catch { setError('知识源已删除，但更新问答选项失败，请刷新页面。') }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败，请重试。')
      setDeleteTarget(undefined)
    } finally { setDeleting(false) }
  }

  return (
    <main className="knowledge-workspace">
      <section className="knowledge-import">
        <span className="section-kicker">USER KNOWLEDGE</span>
        <h1>知识源</h1>
        <p>导入英文 UTF-8 TXT/MD 文件。每份知识源可包含多个文件，用同一组材料进行问答。</p>
        {disabled ? <Alert type="info" message="问答正在运行，完成后可导入、删除或切换知识源。" /> : null}
        <label>导入目标
          <Select aria-label="导入目标" value={targetId ?? 'new'} disabled={locked}
            options={[{ value: 'new', label: '创建新知识源' }, ...items.map((item) => ({ value: item.source_id, label: `${item.name}：导入新版本` }))]}
            onChange={(value) => setTargetId(value === 'new' ? undefined : value)} />
        </label>
        {!targetId ? <label>知识源名称<Input aria-label="知识源名称" maxLength={100} value={name}
          onChange={(event) => setName(event.target.value)} disabled={locked} /></label> : <p>新版本需上传整套文件；已有版本继续保留。</p>}
        <label>材料文件<input key={inputKey} type="file" aria-label="材料文件" accept=".txt,.md" multiple disabled={locked}
          onChange={(event) => setFiles(Array.from(event.target.files ?? []))} /></label>
        <small>最多 10 个文件；单文件 2 MiB；总计 10 MiB。</small>
        {files.length > 0 ? <ul>{files.map((file, i) => <li key={i}>{file.name} · {(file.size / 1024).toFixed(1)} KiB</li>)}</ul> : null}
        <Button type="primary" loading={busy} disabled={locked || !files.length || (!targetId && !name.trim())} onClick={handleImport}>
          {busy ? '正在解析并建索引' : '导入材料'}
        </Button>
        {error ? <Alert type="error" showIcon message="知识源操作失败" description={error} /> : null}
        {notice ? <Alert type="success" showIcon message={notice} /> : null}
      </section>
      <section className="knowledge-library">
        <div className="section-heading"><h2>已导入知识源</h2><Button disabled={locked || loading} onClick={() => refresh(detail?.source_id).catch((reason: Error) => setError(reason.message))}>刷新</Button></div>
        {loading ? <Skeleton active /> : items.length ? <div className="knowledge-source-list">{items.map((item) => (
          <button type="button" key={item.source_id} disabled={busy || deleting} className={detail?.source_id === item.source_id ? 'active' : ''}
            onClick={() => { setDetail(item); setVersion(item.current_version ?? item.versions[0]?.version); setPage(1) }}>
            <strong>{item.name}</strong><Tag color={item.status === 'ready' ? 'success' : 'warning'}>{STATUS[item.status]}</Tag>
            <span>可用版本 {item.current_version ?? '无'} · {item.file_count} 个文件 · {item.chunk_count} 个片段</span>
          </button>
        ))}</div> : <Empty description="还没有用户知识源，请先导入材料。" />}
        {detail ? <article className="knowledge-detail">
          <div className="section-heading"><h2>{detail.name}</h2>
            <Button danger disabled={locked || loading || detail.versions.some(entry => entry.status === 'importing')}
              onClick={() => setDeleteTarget(detail)}>删除知识源</Button>
          </div>
          {detail.versions.some(entry => entry.status === 'importing') ? <p>正在导入，完成后可删除此知识源。</p> : null}
          <Select aria-label="材料版本" disabled={busy || deleting} value={version} options={detail.versions.map((entry) => ({ value: entry.version, label: `版本 ${entry.version} · ${STATUS[entry.status]}` }))}
            onChange={(value) => { setVersion(value); setPage(1) }} />
          <Button type="primary" disabled={locked || !selectedReady || Boolean(previewError) || chunksLoading} onClick={() => { if (version !== undefined) onUse(detail.source_id, version) }}>用于问答</Button>
          {selectedVersion?.error ? <Alert type="error" message={selectedVersion.error.code} description={selectedVersion.error.message} /> : null}
          {selectedVersion?.files ? <ul>{selectedVersion.files.map((file) => <li key={file.filename}>{file.filename} · {(file.size_bytes / 1024).toFixed(1)} KiB</li>)}</ul> : null}
          <h3>片段预览</h3>
          {previewError ? <Alert type="error" message={previewError} /> : chunksLoading && selectedReady ? <Skeleton active /> : chunks?.items.map((chunk) => (
            <article className="knowledge-chunk" key={chunk.chunk_id}>
              <strong>{chunk.filename} · 第 {chunk.line_start}–{chunk.line_end} 行</strong>
              {chunk.heading_path.length ? <small>{chunk.heading_path.join(' / ')}</small> : null}
              <p>{chunk.text}</p>
            </article>
          ))}
          {chunks && chunks.total > 10 ? <Pagination current={page} total={chunks.total} pageSize={10} showSizeChanger={false} onChange={setPage} /> : null}
        </article> : null}
      </section>
      <Modal title="删除知识源" open={Boolean(deleteTarget)} okText="确认删除" cancelText="取消"
        okButtonProps={{ danger: true, disabled: locked }} confirmLoading={deleting}
        cancelButtonProps={{ disabled: deleting }} closable={!deleting} keyboard={!deleting}
        maskClosable={!deleting} onCancel={() => { if (!deleting) setDeleteTarget(undefined) }}
        onOk={() => void handleDelete()}>
        <p>确定删除「{deleteTarget?.name}」及其全部版本的可选入口吗？删除后，它将从知识源列表及新配置选项中移除，也不能再导入新版本。</p>
        <p>已有系统快照及历史记录所需的固定版本材料仍会保留在本地，不会清除。这项操作不会修改已有系统配置。</p>
      </Modal>
    </main>
  )
}
