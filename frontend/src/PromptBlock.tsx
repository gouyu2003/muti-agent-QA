import { memo, useEffect, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { CheckOutlined, CopyOutlined, MenuUnfoldOutlined } from './Icons'
import { Button, Segmented, Tooltip } from 'antd'

const ROLE_COLORS: Record<string, string> = {
  planner: '#0066cc', retriever: '#1f7551', answer_validator: '#935c10', expert: '#7355a5',
}

const FOLD_LINES = 9
type View = 'prompt' | 'json'
type CopyState = 'idle' | 'done' | 'failed'

export interface PromptBlockProps {
  agentId: string
  agentName: string
  role: string
  model: { model_name: string; system_prompt: string; temperature: number; max_tokens: number }
  source?: { source_id: string; name: string; version?: number; sha256: string }
}

/**
 * Read-only viewer for a frozen agent prompt: code-block surface plus an explicit JSON view.
 * Memoized because the draft preview re-renders on every keystroke elsewhere in the editor.
 */
export const PromptBlock = memo(function PromptBlock({ agentId, agentName, role, model, source }: PromptBlockProps) {
  const [view, setView] = useState<View>('prompt')
  const [wrapChoice, setWrapChoice] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState<CopyState>('idle')
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const body = view === 'prompt' ? model.system_prompt : JSON.stringify({
    agent_id: agentId, agent_name: agentName, role, model_config: model,
    ...(source ? { knowledge_source: source } : {}),
  }, null, 2)
  const wrap = wrapChoice ?? view === 'prompt'
  const lines = body ? body.split('\n').length : 0
  const foldable = view === 'prompt' && lines > FOLD_LINES
  const topic = view === 'prompt' ? '系统提示词' : 'JSON 配置'

  async function copy() {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(body)
      else {
        const area = document.createElement('textarea')
        area.value = body
        area.setAttribute('readonly', '')
        area.style.position = 'fixed'
        area.style.opacity = '0'
        document.body.appendChild(area)
        area.select()
        const copiedOk = document.execCommand('copy')
        document.body.removeChild(area)
        if (!copiedOk) throw new Error('copy rejected')
      }
      setCopied('done')
    } catch {
      setCopied('failed')
    }
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setCopied('idle'), 1600)
  }

  function switchView(next: View) {
    setView(next); setExpanded(false); setCopied('idle')
  }

  return <section className={expanded ? 'prompt-block expanded' : 'prompt-block'}
    style={{ '--prompt-color': ROLE_COLORS[role] ?? ROLE_COLORS.planner } as CSSProperties}>
    <header className="prompt-bar">
      <span className="prompt-tag">{view === 'prompt' ? 'PROMPT' : 'JSON'}</span>
      <span className={`prompt-meta ${copied}`} aria-live="polite">
        {copied === 'done' ? '已复制' : copied === 'failed' ? '复制失败，请手动选择文本' : `${lines} 行 · ${body.length} 字符`}
      </span>
      <div className="prompt-tools">
        <div role="group" aria-label={`${agentName} · 展示格式`}>
          <Segmented size="small" value={view} onChange={value => switchView(value as View)}
            options={[{ value: 'prompt', label: '提示词' }, { value: 'json', label: 'JSON' }]} />
        </div>
        <Tooltip title={wrap ? '取消自动换行' : '自动换行'}>
          <Button size="small" type="text" aria-label="自动换行" aria-pressed={wrap} icon={<MenuUnfoldOutlined />}
            onClick={() => setWrapChoice(!wrap)} />
        </Tooltip>
        <Tooltip title="复制提示词">
          <Button size="small" type="text" aria-label="复制提示词" icon={copied === 'done' ? <CheckOutlined /> : <CopyOutlined />}
            onClick={() => void copy()} />
        </Tooltip>
      </div>
    </header>
    <div className={foldable && !expanded ? 'prompt-body masked' : 'prompt-body'} role="group" aria-label={`${agentName} · ${topic}`}>
      <pre tabIndex={0} className={wrap ? 'wrap' : 'nowrap'}><code>{body}</code></pre>
    </div>
    {foldable ? <div className="prompt-foot">
      <Button size="small" type="link" onClick={() => setExpanded(!expanded)}>{expanded ? '收起' : '展开全文'}</Button>
    </div> : null}
  </section>
})
