import { newInstance } from './AgentEditor'
import type { ProtocolSpec } from './protocolTypes'
import type { AgentInstance, AgentTemplate, SystemDraftFields, SystemOptions } from './types'

export function protocolDefaults(spec: ProtocolSpec, templates: AgentTemplate[]) {
  const counts = Object.entries(spec.slots).map(([slot, c]) => ({ slot, c, count: c.role === 'expert' ? Math.max(c.minimum, Math.min(2, c.maximum)) : c.minimum }))
  while (counts.reduce((n, c) => n + c.count, 0) > 5) {
    const extra = counts.find(c => c.count > c.c.minimum)
    if (!extra) throw new Error('协议最少实例超过五个。')
    extra.count--
  }
  const agents: AgentInstance[] = [], bindings: Record<string, string[]> = {}
  for (const { slot, c, count } of counts) {
    const template = templates.find(t => t.agent_type === c.role)
    if (!template) throw new Error(`智能体模板尚未加载：${c.role}`)
    bindings[slot] = []
    for (let i = 0; i < count; i++) {
      const agent = { ...newInstance(template), source_id: null, source_version: null }
      if (c.role === 'expert' && count > 1) agent.name = `专家 ${String.fromCharCode(65 + i)}`
      agents.push(agent); bindings[slot].push(agent.agent_id)
    }
  }
  return { agents, bindings }
}

export function retainedBindings(spec: ProtocolSpec, fields: SystemDraftFields): Record<string, string[]> {
  const used = new Set<string>(), agents = fields.agents ?? []
  return Object.fromEntries(Object.entries(spec.slots).map(([slot, c]) => {
    const legal = agents.filter(a => a.agent_type === c.role && !used.has(a.agent_id))
    let ids = (fields.protocol_bindings?.[slot] ?? []).filter(id => legal.some(a => a.agent_id === id))
    if (ids.length > c.maximum) ids = []
    if (!ids.length && c.minimum === 1 && c.maximum === 1 && legal.length === 1 && Object.values(spec.slots).filter(s => s.role === c.role).length === 1) ids = [legal[0].agent_id]
    ids.forEach(id => used.add(id))
    return [slot, ids]
  }))
}

export function configurationGaps(fields: SystemDraftFields, spec?: ProtocolSpec, options?: SystemOptions): string[] {
  if (!fields.protocol_ref) return fields.workflow_id ? [] : ['请先创建并保存个人协议，再选择执行协议。']
  if (!spec) return ['当前协议冻结配置不可用，请重新读取系统草稿。']
  const agents = fields.agents ?? [], gaps: string[] = [], used = new Set<string>()
  for (const [slot, c] of Object.entries(spec.slots)) {
    const ids = fields.protocol_bindings?.[slot] ?? []
    if (ids.length < c.minimum || ids.length > c.maximum) gaps.push(`${slot}：需要绑定 ${c.minimum}～${c.maximum} 个 ${c.role}，当前 ${ids.length} 个。`)
    for (const id of ids) {
      if (!agents.some(a => a.agent_id === id && a.agent_type === c.role) || used.has(id)) gaps.push(`${slot}：实例绑定无效或重复。`)
      used.add(id)
    }
  }
  if (agents.some(a => !used.has(a.agent_id))) gaps.push('存在未绑定的实例，请按协议要求绑定或移除。')
  for (const a of agents.filter(a => ['expert', 'retriever'].includes(a.agent_type))) {
    if (!a.source_id) gaps.push(`${a.name}：知识源待配置。`)
    else if (options?.sources.find(s => s.source_id === a.source_id)?.source_type === 'document_corpus' && !a.source_version) gaps.push(`${a.name}：请选择固定知识源版本。`)
  }
  return gaps
}
