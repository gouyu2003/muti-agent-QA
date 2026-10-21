import { ArrowDownOutlined, ArrowUpOutlined } from './Icons'
import { Button, Collapse, Input, InputNumber, Select, Space, Tag, Tooltip } from 'antd'
import type { AgentInstance, AgentTemplate, AgentType, SystemOptions } from './types'

export const ROLE_NAMES: Record<AgentType, string> = {
  planner: '规划', retriever: 'BM25 检索', answer_validator: '回答与审核', expert: '领域专家', lead: '主导智能体',
}

export function newInstance(template: AgentTemplate): AgentInstance {
  return { ...structuredClone(template.defaults), agent_id: `agent_${crypto.randomUUID().replaceAll('-', '').slice(0, 16)}` }
}

export function AgentEditor({ agents, templates, options, disabled, onChange, reusableAgents = [] }: {
  agents: AgentInstance[]; templates: AgentTemplate[]; options?: SystemOptions; disabled: boolean
  onChange: (agents: AgentInstance[]) => void
  reusableAgents?: { key: string; label: string; agent: AgentInstance }[]
}) {
  function update(index: number, patch: Partial<AgentInstance>) {
    onChange(agents.map((a, i) => i === index ? { ...a, ...patch } : a))
  }
  return <section className="agent-editor" aria-label="智能体配置">
    <div className="agent-editor-heading"><div><h3>智能体实例</h3><p>各实例配置随系统版本保存。执行顺序由协议阶段与槽位绑定决定。</p></div><Tag>{agents.length} / 5</Tag></div>
    <Space wrap className="agent-editor-add">
      {templates.map(t => <Button key={t.agent_type} disabled={disabled || agents.length >= 5}
        onClick={() => onChange([...agents, newInstance(t)])}>添加{ROLE_NAMES[t.agent_type]}</Button>)}
    </Space>
    {reusableAgents.length ? <label className="agent-copy-source">从其他系统复制实例
      <Select aria-label="从其他系统复制实例" value={null} placeholder="选择要复制的实例；副本独立保存" disabled={disabled || agents.length >= 5}
        options={reusableAgents.map(a => ({ value: a.key, label: a.label }))} onChange={value => {
          const item = reusableAgents.find(a => a.key === value)
          if (item) onChange([...agents, { ...structuredClone(item.agent), agent_id: `agent_${crypto.randomUUID().replaceAll('-', '').slice(0, 16)}` }])
        }} />
    </label> : null}
    {!agents.length ? <p>从上方角色模板添加智能体，再保存草稿。</p> : null}
    <Collapse items={agents.map((a, index) => ({ key: a.agent_id,
      label: <span>{a.name || '未命名实例'} · {ROLE_NAMES[a.agent_type]}</span>,
      children: <div className="agent-instance-fields">
        <small>实例 ID：<code>{a.agent_id}</code></small>
        <label>实例名称<Input aria-label={`${a.agent_id} 名称`} value={a.name} maxLength={100} disabled={disabled}
          onChange={e => update(index, { name: e.target.value })} /></label>
        <label>说明<Input.TextArea aria-label={`${a.agent_id} 说明`} value={a.description} maxLength={2000} disabled={disabled}
          onChange={e => update(index, { description: e.target.value })} /></label>
        {a.model_config ? <>
          <label>模型名称<Input aria-label={`${a.agent_id} 模型`} value={a.model_config.model_name} disabled={disabled}
            onChange={e => update(index, { model_config: { ...a.model_config!, model_name: e.target.value } })} /></label>
          <div className="agent-number-fields">
            <label>温度<InputNumber aria-label={`${a.agent_id} 温度`} min={0} max={2} step={0.1} value={a.model_config.temperature} disabled={disabled}
              onChange={v => update(index, { model_config: { ...a.model_config!, temperature: v ?? 0 } })} /></label>
            <label>Token 上限<InputNumber aria-label={`${a.agent_id} Token 上限`} min={1} max={65536} precision={0} value={a.model_config.max_tokens} disabled={disabled}
              onChange={v => update(index, { model_config: { ...a.model_config!, max_tokens: v ?? 1 } })} /></label>
          </div>
          <label>专属提示词<Input.TextArea aria-label={`${a.agent_id} 提示词`} autoSize={{ minRows: 4, maxRows: 12 }} maxLength={30000}
            value={a.model_config.system_prompt} disabled={disabled}
            onChange={e => update(index, { model_config: { ...a.model_config!, system_prompt: e.target.value } })} /></label>
        </> : <p>此节点使用本地 BM25，不调用模型。</p>}
        {['expert', 'retriever'].includes(a.agent_type) ? <>
          <label>知识源<Select aria-label={`${a.agent_id} 知识源`} allowClear value={a.source_id} disabled={disabled}
            options={options?.sources.map(s => ({ value: s.source_id, label: s.name }))}
            onChange={v => update(index, { source_id: v ?? null, source_version: null })} /></label>
          {options?.sources.find(s => s.source_id === a.source_id)?.source_type === 'document_corpus' ?
            <label>固定材料版本<Select aria-label={`${a.agent_id} 版本`} value={a.source_version} disabled={disabled}
              options={options?.sources.find(s => s.source_id === a.source_id)?.versions?.map(v => ({ value: v, label: `版本 ${v}` }))}
              onChange={v => update(index, { source_version: v })} /></label> : null}
          <label>检索 top_k<InputNumber aria-label={`${a.agent_id} top_k`} min={1} max={50} precision={0} value={a.top_k} disabled={disabled}
            onChange={v => update(index, { top_k: v ?? 1 })} /></label>
        </> : null}
        <Space wrap>
          <Button disabled={disabled || agents.length >= 5} onClick={() => onChange([...agents,
            { ...structuredClone(a), agent_id: `agent_${crypto.randomUUID().replaceAll('-', '').slice(0, 16)}`, name: `${a.name.slice(0, 95)} 副本` }])}>复制实例</Button>
          <Tooltip title="上移">
            <Button aria-label="上移" icon={<ArrowUpOutlined />} disabled={disabled || index === 0} onClick={() => {
              const next = [...agents]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; onChange(next)
            }} />
          </Tooltip>
          <Tooltip title="下移">
            <Button aria-label="下移" icon={<ArrowDownOutlined />} disabled={disabled || index === agents.length - 1} onClick={() => {
              const next = [...agents]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; onChange(next)
            }} />
          </Tooltip>
          <Button danger disabled={disabled || agents.length === 1} onClick={() => onChange(agents.filter((_, i) => i !== index))}>删除实例</Button>
        </Space>
      </div>,
    }))} />
  </section>
}
