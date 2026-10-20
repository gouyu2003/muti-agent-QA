import type { ProtocolSpec } from './protocolTypes'

export function togglePlanner(spec: ProtocolSpec, enabled: boolean, defaults: ProtocolSpec): ProtocolSpec {
  const next = structuredClone(spec)
  const old = next.stages.find(s => s.kind === 'plan')
  if (!enabled && old) {
    const successor = old.routes[0].target
    next.stages = next.stages.filter(s => s.stage_id !== old.stage_id).map(s => ({ ...s,
      reads: s.reads.filter(id => id !== old.stage_id),
      routes: s.routes.map(r => r.target === old.stage_id ? { ...r, target: successor } : r),
      on_limit: s.on_limit === old.stage_id ? successor : s.on_limit,
    }))
    if (next.entry_stage === old.stage_id) next.entry_stage = successor
    delete next.slots[old.slot]
  }
  if (enabled && !old) {
    const stage = structuredClone(defaults.stages.find(s => s.kind === 'plan')!)
    const unique = (base: string, used: string[]) => { let id = base, i = 1; while (used.includes(id)) id = `${base}_${i++}`; return id }
    const originalSlot = stage.slot
    stage.stage_id = unique(stage.stage_id, next.stages.map(s => s.stage_id))
    stage.slot = unique(originalSlot, Object.keys(next.slots))
    stage.routes = [{ condition: 'always', target: next.entry_stage }]
    next.slots[stage.slot] = structuredClone(defaults.slots[originalSlot])
    next.stages = [stage, ...next.stages.map(s => ({ ...s, reads: [...s.reads, stage.stage_id] }))]
    next.entry_stage = stage.stage_id
  }
  return next
}
