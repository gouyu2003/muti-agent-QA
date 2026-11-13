import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { Alert, Button, Drawer, Dropdown, Input, Modal, Select, Skeleton, Tag } from 'antd'
import { DeleteOutlined, EditOutlined, EllipsisOutlined, MenuOutlined, MessageOutlined, PlusOutlined } from '@ant-design/icons'
import { CornerDownLeft, Square } from 'lucide-react'
import AnswerContent from './AnswerContent'
import { cancelBackgroundRun, followBackgroundRun, getManagedSystems, getRunDetail } from './api'
import { RunDetailPanel } from './HistoryView'
import { ChatApiError, clearPendingChat, createConversation, deleteConversation, getChatMessages, getConversation, listConversations,
  readPendingChat, renameConversation, savePendingChat, sendChatMessage } from './chatApi'
import type { ChatMessage, Conversation, PendingChat, ChatSelection } from './chatApi'
import type { ManagedSystem, StoredRunDetail } from './types'
import './chat.css'

const statusLabel = { running: '执行中', completed: '已完成', failed: '执行失败', cancelled: '已停止', interrupted: '执行中断' }
const draftKey = (id?: string) => `maqa.chat.composer.${id || 'new'}`
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。'
const isAbort = (error: unknown) => error instanceof Error && error.name === 'AbortError'
const refLabel = (ref: Conversation['system_ref']) => `${ref.name} · ${ref.kind === 'draft' ? `草稿 r${ref.draft_revision}` : ref.system_version}`

function MessageBody({ message, onDetail }: { message: ChatMessage; onDetail: (id: string) => void }) {
  return <article className={`chat-message chat-message--${message.role}`} aria-label={`${message.role === 'user' ? '你' : '助手'}第 ${message.turn_index} 轮`}>
    <div className="chat-message-label">{message.role === 'user' ? '你' : 'Multi-Agent QA'}</div>
    <div className="chat-message-content">
      {message.role === 'user' ? <p className="chat-user-text">{message.content}</p> : <>
        {message.content ? <AnswerContent result={message.result} content={message.content} intent={message.context.intent}
            showStatus={message.status === 'completed' || !!message.result.response_parts?.length} showReferences />
          : <p className="chat-muted">{message.status === 'running' ? '正在处理你的问题…' : message.error || '本轮未生成回答。'}</p>}
        {message.error && message.content ? <Alert type="error" title={message.error} /> : null}
        <div className="chat-answer-actions">
          {message.status !== 'completed' ? <Tag>{statusLabel[message.status]}</Tag> : null}
          <Button type="text" size="small" onClick={() => onDetail(message.run_id)}>查看本轮运行</Button>
        </div>
      </>}
    </div>
  </article>
}

export default function ChatView() {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const routeRef = useRef(conversationId)
  const chatLoadGeneration = useRef(0)
  routeRef.current = conversationId
  const [list, setList] = useState<Conversation[]>([])
  const [systems, setSystems] = useState<ManagedSystem[]>([])
  const [systemId, setSystemId] = useState<string>()
  const [version, setVersion] = useState<string>()
  const [conversation, setConversation] = useState<Conversation>()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [question, setQuestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [listError, setListError] = useState('')
  const [systemsError, setSystemsError] = useState('')
  const [busyRun, setBusyRun] = useState<string>()
  const [submitting, setSubmitting] = useState(false)
  const submissionRef = useRef(false)
  const [pending, setPending] = useState<PendingChat>()
  const [progress, setProgress] = useState('正在启动')
  const [connection, setConnection] = useState('')
  const [stopping, setStopping] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [editing, setEditing] = useState<Conversation>()
  const [title, setTitle] = useState('')
  const [renameBusy, setRenameBusy] = useState(false)
  const [deleting, setDeleting] = useState<Conversation>()
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [detailId, setDetailId] = useState<string>()
  const [detail, setDetail] = useState<StoredRunDetail>()
  const [detailError, setDetailError] = useState('')
  const [reload, setReload] = useState(0)
  const scroller = useRef<HTMLDivElement>(null)
  const followBottom = useRef(true)

  const refreshList = useCallback(async (signal?: AbortSignal) => {
    try { setList((await listConversations(signal)).items); setListError('') }
    catch (e) { if (!isAbort(e)) setListError(errorText(e)) }
  }, [])
  const refreshSystems = useCallback(async (signal?: AbortSignal) => {
    try { const data = await getManagedSystems(signal); if (!signal?.aborted) { setSystems(data.items); setSystemsError('') } }
    catch (e) { if (!isAbort(e)) setSystemsError(errorText(e)) }
  }, [])
  const loadChat = useCallback(async (id: string, signal?: AbortSignal) => {
    const generation = ++chatLoadGeneration.current
    const [doc, history] = await Promise.all([getConversation(id, signal), getChatMessages(id, signal)])
    if (routeRef.current === id && generation === chatLoadGeneration.current && !signal?.aborted) {
      setConversation(doc); setMessages(history.items)
    }
  }, [])

  const performSubmit = useCallback(async (saved: PendingChat, origin?: string) => {
    if (submissionRef.current) return
    submissionRef.current = true
    setSubmitting(true); setError(''); setBusyRun(undefined)
    let request = { ...saved }
    savePendingChat(request, origin)
    try {
      if (!request.conversationId) {
        const created = await createConversation(request.selection, request.createId)
        request = { ...request, conversationId: created.conversation_id }
        savePendingChat(request, origin)
      }
      await sendChatMessage(request.conversationId!, request.question, request.requestId)
      clearPendingChat(request, origin)
      localStorage.removeItem(draftKey(origin))
      void refreshList()
      if (routeRef.current === origin) {
        setPending(undefined); setQuestion(''); followBottom.current = true
        if (origin && origin === request.conversationId) { await loadChat(origin); setReload(n => n + 1) }
        else navigate(`/chat/${request.conversationId}`)
      }
    } catch (e) {
      if (routeRef.current === origin) {
        if (e instanceof ChatApiError && e.status < 500) {
          clearPendingChat(request, origin); setPending(undefined); setBusyRun(e.activeRunId)
        } else { setPending(request) }
        setError(errorText(e))
      }
    } finally { submissionRef.current = false; setSubmitting(false) }
  }, [loadChat, navigate, refreshList])

  useEffect(() => {
    const ctrl = new AbortController()
    void refreshList(ctrl.signal)
    void refreshSystems(ctrl.signal)
    const onFocus = () => {
      void refreshList()
      if (routeRef.current) void loadChat(routeRef.current).catch(e => setError(errorText(e)))
    }
    window.addEventListener('focus', onFocus)
    const onStorage = (event: StorageEvent) => { if (event.key?.startsWith('maqa.chat.pending.')) onFocus() }
    window.addEventListener('storage', onStorage)
    return () => { ctrl.abort(); window.removeEventListener('focus', onFocus); window.removeEventListener('storage', onStorage) }
  }, [loadChat, refreshList, refreshSystems])

  useEffect(() => {
    const ctrl = new AbortController()
    setConversation(undefined); setMessages([]); setError(''); setBusyRun(undefined); setConnection(''); setStopping(false)
    setQuestion(localStorage.getItem(draftKey(conversationId)) || '')
    followBottom.current = true
    const saved = readPendingChat(conversationId)
    setPending(saved)
    if (saved) { setQuestion(saved.question); void performSubmit(saved, conversationId) }
    void refreshList(ctrl.signal)
    if (conversationId) {
      setLoading(true)
      loadChat(conversationId, ctrl.signal).catch(e => { if (!isAbort(e) && routeRef.current === conversationId) setError(errorText(e)) })
        .finally(() => { if (!ctrl.signal.aborted) setLoading(false) })
    } else { setLoading(false) }
    return () => ctrl.abort()
  }, [conversationId, loadChat, performSubmit, refreshList, reload])

  const activeRun = conversation?.active_run_id
  useEffect(() => {
    if (!activeRun || !conversationId) return
    const ctrl = new AbortController()
    setProgress('正在启动'); setConnection('')
    void followBackgroundRun(activeRun, event => {
      if (ctrl.signal.aborted || routeRef.current !== conversationId || event.run_id !== activeRun) return
      if (event.type === 'run_connection') setConnection('连接中断，正在重新连接；后台任务继续执行。')
      else setConnection('')
      if (event.type === 'node_started') setProgress(`正在执行：${String(event.payload.agent_name || event.payload.node || '智能体')}`)
      if (['run_completed', 'run_failed', 'run_cancelled'].includes(event.type)) {
        void loadChat(conversationId, ctrl.signal).catch(e => { if (!isAbort(e)) setError(errorText(e)) })
        void refreshList(ctrl.signal); setStopping(false)
      }
    }, ctrl.signal).catch(e => { if (!isAbort(e)) setError(errorText(e)) })
    return () => ctrl.abort()
  }, [activeRun, conversationId, loadChat, refreshList])

  useEffect(() => {
    if (followBottom.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
  }, [messages, progress])

  useEffect(() => {
    if (!detailId) return
    const ctrl = new AbortController()
    setDetail(undefined); setDetailError('')
    getRunDetail(detailId, ctrl.signal).then(value => { if (!ctrl.signal.aborted) setDetail(value) }).catch(e => { if (!isAbort(e)) setDetailError(errorText(e)) })
    return () => ctrl.abort()
  }, [detailId])

  const system = systems.find(s => s.system_id === systemId)
  const versionOptions: { value: string; label: string; disabled?: boolean }[] = system ? [
    ...[...system.versions].reverse().map(doc => ({ value: doc.version!, label: `发布版本 ${doc.version}` })),
    { value: `draft:${system.draft_revision}`, label: `已保存草稿 r${system.draft_revision}`, disabled: !system.draft.snapshot.workflow_spec || !!system.draft.snapshot.configuration_errors?.length },
  ] : []
  const selection: ChatSelection | undefined = systemId && version ? { system_id: systemId,
    ...(version.startsWith('draft:') ? { draft_revision: Number(version.slice(6)) } : { system_version: version }) } : undefined
  const selectedDoc = system && (version?.startsWith('draft:') ? system.draft : system.versions.find(doc => doc.version === version))
  const snapshot = conversation?.config_snapshot || selectedDoc?.snapshot
  const sourceNames = snapshot?.sources ? [...new Set(Object.values(snapshot.sources).map(source => `${source.name} · ${source.version ? `v${source.version}` : '固定材料'}`))] : snapshot?.source ? [snapshot.source.name] : []

  function submit() {
    if (!question.trim() || activeRun || submitting || pending || loading) return
    const frozenSelection = conversation ? { system_id: conversation.system_ref.system_id,
      ...(conversation.system_ref.kind === 'draft' ? { draft_revision: conversation.system_ref.draft_revision } : { system_version: conversation.system_ref.system_version }) } : selection
    if (!frozenSelection) { setError('请先选择一个系统及版本。'); return }
    const request: PendingChat = { createId: crypto.randomUUID(), requestId: crypto.randomUUID(), conversationId,
      question: question.trim(), selection: frozenSelection }
    setPending(request); void performSubmit(request, conversationId)
  }

  const sidebar = <div className="chat-sidebar-content">
    <Button icon={<PlusOutlined />} className="chat-new" disabled={!conversationId && (submitting || !!pending)} onClick={() => {
      localStorage.removeItem(draftKey()); setQuestion(''); setSidebarOpen(false)
      if (conversationId) navigate('/chat'); else { setSystemId(undefined); setVersion(undefined); setError('') }
    }}>新聊天</Button>
    <h2>最近会话</h2>
    {listError ? <Alert type="error" title={listError} action={<Button size="small" onClick={() => void refreshList()}>重试</Button>} /> : null}
    <nav className="chat-conversations" aria-label="聊天会话">
      {!list.length ? <p className="chat-muted">发送第一个问题后，会话会保存在这里。</p> : list.map(item => <div className={`chat-conversation ${item.conversation_id === conversationId ? 'is-selected' : ''}`} key={item.conversation_id}>
        <Link to={`/chat/${item.conversation_id}`} onClick={() => setSidebarOpen(false)} aria-current={item.conversation_id === conversationId ? 'page' : undefined}>
          <strong>{item.title}</strong><small>{item.active_run_id ? '运行中 · ' : ''}{item.preview || '尚未发送消息'}</small>
          <time dateTime={item.updated_at}>{new Date(item.updated_at).toLocaleDateString('zh-CN')}</time>
        </Link>
        <Dropdown menu={{ items: [{ key: 'rename', label: '重命名', icon: <EditOutlined /> }, { key: 'delete', label: '删除', icon: <DeleteOutlined />, danger: true }], onClick: ({ key }) => {
          if (key === 'rename') { setEditing(item); setTitle(item.title) } else setDeleting(item)
        } }} trigger={['click']}><Button type="text" size="small" icon={<EllipsisOutlined />} aria-label={`管理会话 ${item.title}`} /></Dropdown>
      </div>)}
    </nav>
  </div>

  return <main className="chat-workspace" aria-label="连续对话">
    <aside className="chat-sidebar">{sidebar}</aside>
    <section className="chat-main">
      <header className="chat-header">
        <Button className="chat-mobile-menu" type="text" icon={<MenuOutlined />} aria-label="打开会话列表" onClick={() => setSidebarOpen(true)} />
        <div><h1>{conversation?.title || '连续对话'}</h1><p>{conversation ? refLabel(conversation.system_ref) : '选择一套系统，开始新的对话'}</p></div>
        {conversation ? <Tag>模型与知识源已固定</Tag> : null}
      </header>
      {conversation && snapshot ? <details className="chat-config-summary"><summary>查看本会话配置</summary>
        <p>协作协议：{snapshot.protocol?.protocol_name || snapshot.workflow_spec?.protocol_id}</p><p>知识源：{sourceNames.join('、') || '未绑定知识源'}</p>
        <p>模型、知识源与协议使用本会话固定配置。聊天策略：{conversation.chat_policy_version || '原有策略'}，新提交按当前策略处理。</p>
      </details> : null}
      <div className="chat-messages" ref={scroller} onScroll={() => {
        const el = scroller.current; if (el) followBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 90
      }}>
        {loading ? <Skeleton active paragraph={{ rows: 5 }} /> : !messages.length ? <div className="chat-welcome">
          <MessageOutlined className="chat-welcome-icon" /><h2>今天想了解什么？</h2><p>在同一个会话里接着提问，回答会结合前面的对话。</p>
          {!conversationId ? <div className="chat-selection">
            {systemsError ? <Alert type="error" title={systemsError} action={<Button size="small" onClick={() => void refreshSystems()}>重试加载系统</Button>} /> : null}
            <label>问答系统<Select aria-label="问答系统" placeholder="选择我的系统" value={systemId} disabled={submitting || !!pending} options={systems.map(s => ({ value: s.system_id, label: s.draft.name }))} onChange={id => {
              setSystemId(id); const selected = systems.find(s => s.system_id === id)!
              setVersion(selected.versions.at(-1)?.version || `draft:${selected.draft_revision}`)
            }} /></label>
            <label>系统版本<Select aria-label="系统版本" placeholder="选择版本" value={version} disabled={!systemId || submitting || !!pending} options={versionOptions} onChange={setVersion} /></label>
            {snapshot ? <p>协作协议：{snapshot.protocol?.protocol_name || snapshot.workflow_spec?.protocol_id}<br />知识源：{sourceNames.join('、') || '未绑定知识源'}</p> : null}
            {selectedDoc?.snapshot.configuration_errors?.length ? <p>这个草稿尚不可运行，请到<Link to="/systems">我的系统</Link>完成配置后重新选择。</p> : null}
            {!systems.length && !systemsError ? <p>还没有可选择的系统，先到<Link to="/systems">我的系统</Link>完成配置。</p> : null}
          </div> : null}
        </div> : <div className="chat-message-list">{messages.map(message => <MessageBody message={message} key={message.message_id} onDetail={setDetailId} />)}</div>}
      </div>
      <div className="chat-composer-area">
        {error ? <Alert type="error" title={error} closable onClose={() => setError('')} action={busyRun ? <Button size="small" onClick={() => setDetailId(busyRun)}>查看正在运行的任务</Button> : undefined} /> : null}
        {pending && !submitting ? <Alert type="warning" title="上次提交尚未确认，重试会沿用原提交编号。" action={<Button onClick={() => void performSubmit(pending, conversationId)}>重试提交</Button>} /> : null}
        {connection ? <p className="chat-runtime-note" role="status">{connection}</p> : activeRun ? <p className="chat-runtime-note" role="status">{stopping ? '正在停止…' : progress}</p> : null}
        <form className="chat-composer" onSubmit={e => { e.preventDefault(); submit() }}>
          <Input.TextArea variant="borderless" aria-label="聊天消息" placeholder="回复" value={question} maxLength={2000} autoSize={{ minRows: 1, maxRows: 7 }}
            disabled={submitting || !!pending || loading || !!conversationId && !conversation} onChange={e => { setQuestion(e.target.value); localStorage.setItem(draftKey(conversationId), e.target.value) }} onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); submit() }
            }} />
          {activeRun ? <Button type="text" className="chat-composer-send" aria-label="停止回答" icon={<Square size={13} strokeWidth={0} fill="currentColor" />} loading={stopping} onClick={() => {
            setStopping(true); cancelBackgroundRun(activeRun).catch(e => { setError(errorText(e)); setStopping(false) })
          }} /> : <Button type="text" className={`chat-composer-send${question.trim() ? ' is-ready' : ''}`} htmlType="submit" aria-label="发送消息" icon={<CornerDownLeft size={18} strokeWidth={1.7} />} loading={submitting}
            disabled={!question.trim() || !!pending || loading || !!conversationId && !conversation || !conversationId && (!selection || !!versionOptions.find(v => v.value === version)?.disabled)} />}
        </form>
        <p className="chat-composer-hint">支持通用对话与知识库问答。知识库部分请结合引用核对，每轮执行详情均会保存。</p>
      </div>
    </section>
    <Drawer title="聊天会话" placement="left" open={sidebarOpen} onClose={() => setSidebarOpen(false)} size={290}>{sidebar}</Drawer>
    <Drawer title="本轮运行" open={!!detailId} onClose={() => setDetailId(undefined)} size="min(960px, 100vw)">
      {detailError ? <Alert type="error" title={detailError} /> : detail ? <RunDetailPanel detail={detail} /> : <Skeleton active />}
    </Drawer>
    <Modal title="重命名会话" open={!!editing} confirmLoading={renameBusy} onCancel={() => setEditing(undefined)} okButtonProps={{ disabled: !title.trim() }} onOk={async () => {
      if (!editing) return
      setRenameBusy(true)
      try { await renameConversation(editing.conversation_id, title.trim()); setEditing(undefined); await refreshList(); if (editing.conversation_id === conversationId) await loadChat(conversationId) }
      catch (e) { setError(errorText(e)) } finally { setRenameBusy(false) }
    }}><Input aria-label="会话标题" value={title} maxLength={100} onChange={e => setTitle(e.target.value)} /></Modal>
    <Modal title="删除这个会话？" open={!!deleting} confirmLoading={deleteBusy} okText="删除" okButtonProps={{ danger: true }} onCancel={() => setDeleting(undefined)} onOk={async () => {
      if (!deleting) return
      setDeleteBusy(true)
      try { await deleteConversation(deleting.conversation_id); setDeleting(undefined); await refreshList(); if (deleting.conversation_id === conversationId) navigate('/chat') }
      catch (e) { setError(errorText(e)); setDeleting(undefined) } finally { setDeleteBusy(false) }
    }}><p>会话和消息将被删除，关联的运行记录仍保留在运行历史中。</p>{deleting?.active_run_id ? <Alert type="warning" title="这个会话仍在运行，请先停止再删除。" /> : null}</Modal>
  </main>
}
