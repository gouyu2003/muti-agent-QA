import { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import {
  ApartmentOutlined,
  ArrowRightOutlined,
  BranchesOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  CloseCircleFilled,
  DatabaseOutlined,
  FileSearchOutlined,
  HistoryOutlined,
  LoadingOutlined,
  PauseCircleOutlined,
  PlayCircleFilled,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  StopOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Badge,
  Button,
  Card,
  Collapse,
  Descriptions,
  Empty,
  Input,
  Progress,
  Select,
  Skeleton,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ReactNode } from 'react'
import { getSystemInfo, getSystemOptions, streamQuestion } from './api'
import HistoryView from './HistoryView'
import KnowledgeView from './KnowledgeView'
import EvidenceLocation from './EvidenceLocation'
import { initialRunState, runReducer } from './runState'
import type {
  AgentInfo,
  EvidenceGroup,
  NodeId,
  RunResult,
  RunStatus,
  SystemInfo,
  SystemOptions,
  WorkflowOption,
  SourceOption,
  TraceStep,
} from './types'

const { TextArea } = Input
const { Paragraph, Text, Title } = Typography

const DEFAULT_QUESTION =
  'What government position was held by the woman who portrayed Corliss Archer in the film Kiss and Tell?'

const NODE_ORDER: NodeId[] = ['planner_v0', 'retriever_v0', 'answer_validator_v0']

const NODE_META: Record<
  NodeId,
  { shortName: string; english: string; color: string; icon: ReactNode }
> = {
  planner_v0: {
    shortName: '规划智能体',
    english: 'PLANNER',
    color: '#3157d5',
    icon: <BranchesOutlined />,
  },
  retriever_v0: {
    shortName: '检索智能体',
    english: 'RETRIEVER',
    color: '#138a8a',
    icon: <SearchOutlined />,
  },
  answer_validator_v0: {
    shortName: '回答与验证智能体',
    english: 'ANSWER · VALIDATOR',
    color: '#c77816',
    icon: <SafetyCertificateOutlined />,
  },
}

function formatDuration(value?: number) {
  if (value === undefined) return '—'
  if (value < 1000) return `${value} ms`
  return `${(value / 1000).toFixed(1)} s`
}

function statusCopy(status: RunStatus) {
  const copies: Record<RunStatus, string> = {
    idle: '等待运行',
    running: '协同执行中',
    completed: '执行完成',
    failed: '执行失败',
    cancelled: '已停止',
  }
  return copies[status]
}

function ProtocolRail({
  agents,
  workflow,
  activeNode,
  steps,
}: {
  agents: AgentInfo[]
  workflow?: WorkflowOption
  activeNode?: NodeId
  steps: TraceStep[]
}) {
  const agentMap = new Map(agents.map((agent) => [agent.agent_id, agent]))
  const feedbackEdges = workflow?.edges.filter((edge) =>
    edge.route_name === 'insufficient_retry' || edge.route_name === 'conflict_retry') ?? []

  return (
    <section className="protocol-rail" aria-label="三智能体协同协议">
      <div className="section-heading compact">
        <div>
          <span className="section-kicker">RUNTIME PROTOCOL</span>
          <h2>协同协议轨道</h2>
        </div>
        <Tag variant="filled" className="version-tag">
          {workflow?.name ?? '选择协作流程'}
        </Tag>
      </div>

      <div className="agent-track">
        {NODE_ORDER.map((node, index) => {
          const meta = NODE_META[node]
          const agent = agentMap.get(node)
          const hasCompleted = steps.some((step) => step.node === node && step.status === 'completed')
          const state = activeNode === node ? 'active' : hasCompleted ? 'completed' : 'waiting'
          const cycles = steps.filter((step) => step.node === node).length

          return (
            <div className="agent-track-item" key={node}>
              <article
                className={`agent-node ${state}`}
                style={{ '--agent-color': meta.color } as React.CSSProperties}
                aria-current={state === 'active' ? 'step' : undefined}
              >
                <div className="agent-node-icon">{meta.icon}</div>
                <div className="agent-node-copy">
                  <span>{meta.english}</span>
                  <strong>{agent?.name ?? meta.shortName}</strong>
                  <small>
                    {state === 'active'
                      ? '正在处理当前状态'
                      : state === 'completed'
                        ? `已完成${cycles > 1 ? ` · 第 ${cycles} 轮` : ''}`
                        : index === 0
                          ? '负责拆解问题与规划路径'
                          : index === 1
                            ? '负责检索与组织证据'
                            : '负责作答、校验与路由'}
                  </small>
                </div>
                <div className={`agent-node-status ${state}`}>
                  {state === 'active' ? <LoadingOutlined spin /> : null}
                  {state === 'completed' ? <CheckCircleFilled /> : null}
                  {state === 'waiting' ? <span /> : null}
                </div>
              </article>
              {index < NODE_ORDER.length - 1 ? (
                <div className={`track-connector ${activeNode === NODE_ORDER[index + 1] ? 'active' : ''}`}>
                  <ArrowRightOutlined />
                </div>
              ) : null}
            </div>
          )
        })}
      </div>

      <div className="feedback-routes">
        {feedbackEdges.length ? feedbackEdges.map((edge) => (
          <div key={edge.route_name}>
            {edge.route_name === 'insufficient_retry' ? <ReloadOutlined /> : <BranchesOutlined />}
            <span>{edge.route_name === 'insufficient_retry' ? '证据不足' : '证据冲突'}</span>
            <strong>{edge.route_name === 'insufficient_retry' ? '返回检索' : '重新规划'}</strong>
          </div>
        )) : <p>固定顺序执行一次，验证后结束。</p>}
      </div>
      {workflow && Object.keys(workflow.retry_limits).length > 0 ? (
        <p className="protocol-limits">
          重试上限：{Object.entries(workflow.retry_limits).map(([key, value]) => `${key} ${value}`).join(' · ')}
        </p>
      ) : null}
    </section>
  )
}

function PlannerOutput({ result }: { result: Partial<RunResult> }) {
  const questions = result.sub_questions ?? []
  return (
    <div className="trace-details">
      <div className="trace-detail-summary">
        <Tag color="blue">{questions.length} 个子问题</Tag>
        <Text type="secondary">策略：{result.retrieval_strategy ?? 'sequential'}</Text>
      </div>
      {questions.map((item) => (
        <div className="subquestion-row" key={item.id}>
          <span>{item.id}</span>
          <div>
            <strong>{item.question}</strong>
            <small>
              {item.keywords.length ? `关键词：${item.keywords.join(' / ')}` : '使用问题文本检索'}
              {item.depends_on.length ? ` · 依赖：${item.depends_on.join(', ')}` : ''}
            </small>
          </div>
        </div>
      ))}
    </div>
  )
}

function RetrieverOutput({ result }: { result: Partial<RunResult> }) {
  const groups = result.evidence_chunks ?? []
  const chunkCount = groups.reduce((total, group) => total + group.chunks.length, 0)
  return (
    <div className="trace-details">
      <div className="trace-detail-summary">
        <Tag color="cyan">{chunkCount} 条候选证据</Tag>
        <Text type="secondary">覆盖 {groups.length} 个子问题</Text>
      </div>
      {groups.map((group) => (
        <div className="retrieval-group" key={group.sq_id}>
          <div>
            <strong>{group.sq_id}</strong>
            <Tag color={group.sufficiency === 'sufficient' ? 'success' : 'warning'} variant="filled">
              {group.sufficiency === 'sufficient' ? '证据充分' : '证据不足'}
            </Tag>
          </div>
          <Text type="secondary">
            {group.chunks.length
              ? `已返回 ${group.chunks.length} 条排序结果`
              : group.insufficiency_note || '未检索到证据'}
          </Text>
        </div>
      ))}
    </div>
  )
}

function ValidatorOutput({ result }: { result: Partial<RunResult> }) {
  return (
    <div className="trace-details">
      <div className="trace-detail-summary">
        <Tag color={result.verified ? 'success' : 'warning'}>
          {result.verified ? '验证通过' : `未通过 · ${result.failure_type ?? 'unknown'}`}
        </Tag>
      </div>
      {result.answer ? <Paragraph className="trace-answer">{result.answer}</Paragraph> : null}
      <Text type="secondary">{result.verification_note || '验证智能体未返回说明'}</Text>
    </div>
  )
}

function StepOutput({ step }: { step: TraceStep }) {
  if (!step.output) return <Skeleton active paragraph={{ rows: 2 }} />
  if (step.node === 'planner_v0') return <PlannerOutput result={step.output} />
  if (step.node === 'retriever_v0') return <RetrieverOutput result={step.output} />
  return <ValidatorOutput result={step.output} />
}

function ExecutionTrace({ steps }: { steps: TraceStep[] }) {
  const [openKeys, setOpenKeys] = useState<string[]>([])

  useEffect(() => {
    const latest = steps.at(-1)?.key
    if (!latest) return
    setOpenKeys((current) => (current.includes(latest) ? current : [...current, latest]))
  }, [steps])

  if (steps.length === 0) {
    return (
      <div className="trace-empty">
        <ApartmentOutlined />
        <strong>运行后将在这里生成协同轨迹</strong>
        <span>每个节点都来自 LangGraph 的真实运行事件</span>
      </div>
    )
  }

  const items = steps.map((step, index) => {
    const meta = NODE_META[step.node]
    return {
      key: step.key,
      label: (
        <div className="trace-label">
          <span className="trace-index">{String(index + 1).padStart(2, '0')}</span>
          <span className="trace-node-icon" style={{ color: meta.color }}>
            {step.status === 'running' ? <LoadingOutlined spin /> : meta.icon}
          </span>
          <div>
            <strong>{meta.shortName}</strong>
            <small>
              第 {step.cycle} 轮 · {step.status === 'running' ? '执行中' : '已完成'}
            </small>
          </div>
        </div>
      ),
      children: <StepOutput step={step} />,
      className: `trace-collapse-item ${step.status}`,
    }
  })

  return (
    <div className="trace-list">
      <Collapse
        ghost
        items={items}
        activeKey={openKeys}
        onChange={(keys) => setOpenKeys(Array.isArray(keys) ? keys.map(String) : [String(keys)])}
        expandIconPosition="end"
      />
    </div>
  )
}

function EvidenceLedger({ groups, source }: { groups: EvidenceGroup[]; source?: SourceOption }) {
  if (!groups.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无检索证据" />

  return (
    <div className="evidence-ledger">
      {groups.map((group) => (
        <section className="evidence-group" key={group.sq_id}>
          <header>
            <div>
              <FileSearchOutlined />
              <strong>{group.sq_id}</strong>
            </div>
            <Tag color={group.sufficiency === 'sufficient' ? 'success' : 'warning'} variant="filled">
              {group.sufficiency === 'sufficient' ? '充分' : '不足'}
            </Tag>
          </header>
          <div className="evidence-items">
            {group.chunks.map((chunk, index) => (
              <article className="evidence-item" key={`${chunk.source_title}-${chunk.source_paragraph}-${index}`}>
                <div className="evidence-source">
                  <DatabaseOutlined />
                  <span>{chunk.source_title}</span>
                  {!chunk.filename ? <code>sentence #{chunk.source_paragraph}</code> : null}
                  <Tooltip title="BM25 相对相关性得分">
                    <Tag variant="filled">score {chunk.relevance_score.toFixed(2)}</Tag>
                  </Tooltip>
                </div>
                <EvidenceLocation evidence={chunk} source={source} />
                <p>{chunk.text}</p>
              </article>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

function ResultPanel({ result, durationMs, source }: { result?: RunResult; durationMs?: number; source?: SourceOption }) {
  if (!result) {
    return (
      <div className="result-empty">
        <SafetyCertificateOutlined />
        <span>完成问答后，这里会显示答案、验证结论和证据账本。</span>
      </div>
    )
  }

  const verified = Boolean(result.verified)
  return (
    <div className="result-content">
      <div className={`answer-hero ${verified ? 'verified' : 'unverified'}`}>
        <div className="answer-verdict">
          {verified ? <CheckCircleFilled /> : <CloseCircleFilled />}
          <span>{verified ? 'EVIDENCE VERIFIED' : 'VERIFICATION INCOMPLETE'}</span>
        </div>
        <Title level={2}>{result.answer || '未生成可用答案'}</Title>
        <Paragraph>{result.verification_note || '验证智能体未提供说明。'}</Paragraph>
        <div className="answer-metrics">
          <span>
            <ClockCircleOutlined /> {formatDuration(durationMs)}
          </span>
          <span>
            <ReloadOutlined /> 补检索 {result.retrieval_retry_count ?? 0} 次
          </span>
          <span>
            <BranchesOutlined /> 重规划 {result.plan_retry_count ?? 0} 次
          </span>
        </div>
      </div>

      <div className="result-evidence-heading">
        <div>
          <span className="section-kicker">EVIDENCE LEDGER</span>
          <h3>检索证据账本</h3>
        </div>
        <Tag>{result.evidence_chunks?.reduce((sum, group) => sum + group.chunks.length, 0) ?? 0} 条</Tag>
      </div>
      <EvidenceLedger groups={result.evidence_chunks ?? []} source={source} />
      {result.supporting_evidence?.length ? <section className="supporting-evidence">
        <h3>最终支撑证据</h3>
        {result.supporting_evidence.map((evidence, index) => <article key={`${evidence.sq_id}-${evidence.chunk_id ?? index}-${index}`}>
          <strong>{evidence.source_title}</strong><EvidenceLocation evidence={evidence} source={source} /><p>{evidence.text}</p>
        </article>)}
      </section> : null}
    </div>
  )
}

export default function App() {
  const [view, setView] = useState<'live' | 'history' | 'knowledge'>('live')
  const [system, setSystem] = useState<SystemInfo>()
  const [options, setOptions] = useState<SystemOptions>()
  const [workflowId, setWorkflowId] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [sourceVersion, setSourceVersion] = useState<number>()
  const [submittedSelection, setSubmittedSelection] = useState<{ workflowId: string; sourceId: string; sourceVersion?: number }>()
  const [submittedSource, setSubmittedSource] = useState<SourceOption>()
  const [systemError, setSystemError] = useState('')
  const [question, setQuestion] = useState(DEFAULT_QUESTION)
  const questionTouchedRef = useRef(false)
  const [run, dispatch] = useReducer(runReducer, initialRunState)
  const abortRef = useRef<AbortController | undefined>(undefined)

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([getSystemInfo(controller.signal), getSystemOptions(controller.signal)])
      .then(([info, choices]) => {
        if (!choices.workflows.some((item) => item.workflow_id === choices.default_workflow_id)
          || !choices.sources.some((item) => item.source_id === choices.default_source_id)) {
          throw new Error('默认流程或知识源不在可选配置中。')
        }
        setSystem(info)
        setOptions(choices)
        setWorkflowId(choices.default_workflow_id)
        setSourceId(choices.default_source_id)
        setSourceVersion(choices.sources.find((item) => item.source_id === choices.default_source_id)?.version ?? undefined)
        if (!questionTouchedRef.current) {
          const source = choices.sources.find((item) => item.source_id === choices.default_source_id)
          setQuestion(source?.example_question ?? info.dataset.default_question)
        }
        setSystemError('')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setSystemError(error instanceof Error ? error.message : '后端连接失败')
      })
    return () => controller.abort()
  }, [])

  const agents = useMemo(
    () =>
      system?.agents ??
      NODE_ORDER.map((node) => ({
        agent_id: node,
        name: NODE_META[node].shortName,
        role: node,
        description: '',
      })),
    [system],
  )
  const currentWorkflow = options?.workflows.find((item) => item.workflow_id === workflowId)
  const sourceOption = options?.sources.find((item) => item.source_id === sourceId)
  const versionInfo = sourceOption?.version_details?.find((item) => item.version === sourceVersion)
  const currentSource = sourceOption ? { ...sourceOption, ...(versionInfo ?? {}) } : undefined
  const shownWorkflow = submittedSelection && run.status !== 'idle'
    ? options?.workflows.find((item) => item.workflow_id === submittedSelection.workflowId)
    : currentWorkflow
  const shownSource = submittedSelection && run.status !== 'idle'
    ? submittedSource
    : currentSource

  function selectWorkflow(value: string) {
    setWorkflowId(value)
    setSubmittedSelection(undefined)
    dispatch({ type: 'clear' })
  }

  function selectSource(value: string) {
    setSourceId(value)
    setSourceVersion(options?.sources.find((item) => item.source_id === value)?.version ?? undefined)
    setSubmittedSelection(undefined)
    dispatch({ type: 'clear' })
  }

  async function refreshOptions() {
    try {
      setOptions(await getSystemOptions())
      setSystemError('')
    } catch (reason) {
      setSystemError(reason instanceof Error ? reason.message : '配置选项加载失败')
      throw reason
    }
  }

  function useKnowledge(value: string, version: number) {
    setSourceId(value)
    setSourceVersion(version)
    setSubmittedSelection(undefined)
    dispatch({ type: 'clear' })
    setView('live')
  }

  async function handleRun() {
    const normalized = question.trim()
    if (!normalized) {
      dispatch({ type: 'client_error', message: '请输入问题后再运行。' })
      return
    }
    if (!currentWorkflow || !currentSource) {
      dispatch({ type: 'client_error', message: '所选流程或知识源不可用，请重新加载配置。' })
      return
    }

    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const selection = { workflowId: currentWorkflow.workflow_id, sourceId: currentSource.source_id,
      ...(sourceVersion !== undefined ? { sourceVersion } : {}) }
    setSubmittedSelection(selection)
    setSubmittedSource(currentSource)
    dispatch({ type: 'reset' })

    try {
      await streamQuestion(
        normalized,
        (event) => dispatch({ type: 'stream_event', event }),
        controller.signal,
        selection,
      )
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      dispatch({
        type: 'client_error',
        message: error instanceof Error ? error.message : '无法连接问答服务。',
      })
    }
  }

  function handleStop() {
    abortRef.current?.abort()
    dispatch({ type: 'cancel' })
  }

  const isRunning = run.status === 'running'
  const completedSteps = run.steps.filter((step) => step.status === 'completed').length

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark">
            <ApartmentOutlined />
          </div>
          <div>
            <strong>Multi-Agent QA</strong>
            <span>可配置多智能体协同问答系统</span>
          </div>
        </div>
        <nav className="workspace-nav" aria-label="工作区视图">
          <button
            type="button" className={view === 'live' ? 'active' : ''}
            aria-current={view === 'live' ? 'page' : undefined}
            onClick={() => setView('live')}
          >
            <PlayCircleFilled /> 实时运行
          </button>
          <button
            type="button" className={view === 'history' ? 'active' : ''}
            aria-current={view === 'history' ? 'page' : undefined}
            onClick={() => setView('history')}
          >
            <HistoryOutlined /> 运行历史
          </button>
          <button type="button" className={view === 'knowledge' ? 'active' : ''}
            aria-current={view === 'knowledge' ? 'page' : undefined} onClick={() => setView('knowledge')}>
            <DatabaseOutlined /> 知识源
          </button>
        </nav>
        <div className="system-badges">
          <Tag variant="filled">{shownWorkflow?.workflow_id ?? '等待配置'}</Tag>
          <Tag variant="filled">{system?.model_name ?? 'DeepSeek · Bailian'}</Tag>
          <Badge status={systemError ? 'error' : options ? 'success' : 'processing'} text={systemError ? '配置不可用' : options ? '服务正常' : '正在连接'} />
        </div>
      </header>

      {view === 'history' ? <HistoryView /> : view === 'knowledge' ? (
        <KnowledgeView disabled={isRunning} onChanged={refreshOptions} onUse={useKnowledge} />
      ) : <main className="workspace">
        <aside className="protocol-column">
          <ProtocolRail agents={agents} workflow={shownWorkflow} activeNode={run.activeNode} steps={run.steps} />

          <Card className="dataset-card" variant="borderless">
            <div className="dataset-icon">
              <DatabaseOutlined />
            </div>
            <div>
              <span className="section-kicker">KNOWLEDGE SOURCE</span>
              <strong>{shownSource?.name ?? '等待知识源'}</strong>
              {shownSource?.is_demo ? <Tag color="orange">演示材料</Tag> : null}
              <p>
                {shownSource?.source_type === 'document_corpus'
                  ? `版本 ${shownSource.version} · ${shownSource.file_count} 个文件 · ${shownSource.chunk_count} 个片段`
                  : shownSource
                  ? `${shownSource.level} · ${shownSource.question_type} · ${shownSource.context_count} 个上下文 · ${shownSource.supporting_fact_count} 条支撑事实`
                  : '正在加载本地知识源'}
              </p>
              {shownSource ? <small>来源：{shownSource.source_id}{shownSource.record_id ? ` · 记录：${shownSource.record_id}` : ''}</small> : null}
            </div>
          </Card>
        </aside>

        <div className="main-column">
          <section className="intro-panel">
            <div>
              <span className="section-kicker">QUESTION TO EVIDENCE</span>
              <Title level={1}>观察一次真实的多智能体协作</Title>
              <Paragraph>
                系统会拆解问题、检索所选知识源，并按照选定的协作流程作答与验证。
              </Paragraph>
            </div>
            <div className="run-summary">
              <div>
                <span>运行状态</span>
                <strong>{statusCopy(run.status)}</strong>
              </div>
              <div>
                <span>已完成节点</span>
                <strong>{completedSteps}</strong>
              </div>
              <div>
                <span>总耗时</span>
                <strong>{formatDuration(run.durationMs)}</strong>
              </div>
            </div>
          </section>

          {systemError ? (
            <Alert
              type="error"
              showIcon
              message="系统配置加载失败"
              description={systemError}
            />
          ) : null}

          <Card className="question-card" variant="borderless">
            <div className="configuration-controls">
              <label>
                <span>协作流程</span>
                <Select
                  aria-label="协作流程"
                  value={workflowId || undefined}
                  options={options?.workflows.map((item) => ({ value: item.workflow_id, label: item.name })) ?? []}
                  onChange={selectWorkflow}
                  disabled={isRunning || !options}
                  placeholder="选择协作流程"
                />
              </label>
              <label>
                <span>知识源</span>
                <Select
                  aria-label="知识源"
                  value={sourceId || undefined}
                  options={options?.sources.map((item) => ({ value: item.source_id, label: item.is_demo ? `${item.name}（演示）` : item.name })) ?? []}
                  onChange={selectSource}
                  disabled={isRunning || !options}
                  placeholder="选择知识源"
                />
              </label>
              {currentSource?.source_type === 'document_corpus' ? <label>
                <span>知识源版本</span>
                <Select aria-label="知识源版本" value={sourceVersion}
                  options={currentSource.versions?.map((version) => ({ value: version, label: `版本 ${version}` }))}
                  disabled={isRunning}
                  onChange={(version) => { setSourceVersion(version); setSubmittedSelection(undefined); dispatch({ type: 'clear' }) }} />
              </label> : null}
            </div>
            {currentWorkflow ? (
              <div className="configuration-preview">
                <strong>{currentWorkflow.name}</strong>
                <span>方法：{currentWorkflow.method_id} · 入口：{currentWorkflow.entry_node}</span>
                {currentWorkflow.description ? <p>{currentWorkflow.description}</p> : null}
                <p>连线：{currentWorkflow.edges.map((edge) => `${edge.source} → ${edge.target}（${edge.route_name}）`).join('；')}</p>
              </div>
            ) : null}
            <div className="question-card-heading">
              <div>
                <RobotOutlined />
                <strong>输入复杂多跳问题</strong>
              </div>
              <Text type="secondary">当前知识源使用英文问题与本地英文语料</Text>
            </div>
            <TextArea
              value={question}
              onChange={(event) => { questionTouchedRef.current = true; setQuestion(event.target.value) }}
              autoSize={{ minRows: 3, maxRows: 6 }}
              maxLength={2000}
              disabled={isRunning}
              aria-label="多跳问答问题"
            />
            <div className="question-actions">
              <Text type="secondary">{question.length} / 2000</Text>
              <Button
                onClick={() => { if (currentSource?.example_question) { questionTouchedRef.current = true; setQuestion(currentSource.example_question) } }}
                disabled={isRunning || !currentSource?.example_question}
              >填入示例问题</Button>
              {isRunning ? (
                <Button icon={<StopOutlined />} onClick={handleStop} danger>
                  停止运行
                </Button>
              ) : (
                <Button
                  type="primary"
                  icon={<PlayCircleFilled />}
                  onClick={handleRun}
                  disabled={Boolean(systemError) || !options || !currentWorkflow || !currentSource || !question.trim()}
                >
                  开始协同问答
                </Button>
              )}
            </div>
          </Card>

          {submittedSelection && run.status !== 'idle' ? (
            <p className="run-selection">本次运行：{shownWorkflow?.name ?? submittedSelection.workflowId} · {shownSource?.name ?? submittedSelection.sourceId}{submittedSelection.sourceVersion ? ` · 版本 ${submittedSelection.sourceVersion}` : ''}</p>
          ) : null}

          {run.error ? <Alert type="error" showIcon message="运行未完成" description={run.error} /> : null}
          {run.status === 'cancelled' ? (
            <Alert type="info" showIcon icon={<PauseCircleOutlined />} message="本次运行已停止，可修改问题后重新开始。" />
          ) : null}

          <div className="content-grid">
            <Card className="trace-card" variant="borderless">
              <div className="section-heading">
                <div>
                  <span className="section-kicker">LIVE EXECUTION TRACE</span>
                  <h2>实时执行轨迹</h2>
                </div>
                {isRunning ? <Tag icon={<LoadingOutlined spin />} color="processing">运行中</Tag> : null}
              </div>
              {isRunning ? (
                <Progress
                  percent={Math.min(92, 18 + completedSteps * 25)}
                  showInfo={false}
                  strokeColor="#3157d5"
                  trailColor="#e8edf4"
                  className="run-progress"
                />
              ) : null}
              <ExecutionTrace steps={run.steps} />
            </Card>

            <Card className="result-card" variant="borderless">
              <div className="section-heading">
                <div>
                  <span className="section-kicker">FINAL RESPONSE</span>
                  <h2>答案与验证</h2>
                </div>
                {run.result ? (
                  <Tag color={run.result.verified ? 'success' : 'warning'}>
                    {run.result.verified ? '证据已验证' : run.result.failure_type ?? '未验证'}
                  </Tag>
                ) : null}
              </div>
              <ResultPanel result={run.result} durationMs={run.durationMs} source={shownSource} />
            </Card>
          </div>
        </div>
      </main>}
    </div>
  )
}
