"""Version-one, server-owned protocol language. No executable user expressions."""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from src.runtime.specs import StrictSpec

Role = Literal['planner', 'retriever', 'expert', 'answer_validator', 'lead']
Kind = Literal['plan', 'retrieve', 'propose', 'critique', 'revise', 'lead', 'dispatch', 'review']
Condition = Literal['always', 'verified', 'unverified', 'lead_dispatch', 'lead_review', 'fallback']
CAPABILITIES = {'plan': 'planner', 'retrieve': 'retriever', 'propose': 'expert',
                'critique': 'expert', 'revise': 'expert', 'lead': 'lead',
                'dispatch': 'expert', 'review': 'answer_validator'}


class Slot(StrictSpec):
    role: Role
    minimum: StrictInt = Field(default=1, ge=1, le=3)
    maximum: StrictInt = Field(default=1, ge=1, le=3)

    @model_validator(mode='after')
    def limits(self):
        if self.minimum > self.maximum or (self.role != 'expert' and self.maximum != 1):
            raise ValueError('角色人数范围无效；只有专家槽位可以绑定多个实例。')
        return self


class Transition(StrictSpec):
    condition: Condition = 'always'
    target: str = Field(pattern=r'^(END|[A-Za-z][A-Za-z0-9_-]{0,63})$')


class Stage(StrictSpec):
    stage_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    kind: Kind
    slot: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    mode: Literal['sequential', 'parallel'] = 'sequential'
    reads: list[str] = Field(default_factory=list, max_length=12)
    routes: list[Transition] = Field(min_length=1, max_length=4)
    max_visits: StrictInt = Field(default=1, ge=1, le=5)
    on_limit: str = Field(default='END', pattern=r'^(END|[A-Za-z][A-Za-z0-9_-]{0,63})$')
    instructions: str = Field(default='', max_length=4000)
    share_evidence: bool = True
    peer_policy: Literal['all', 'next'] = 'all'


class Limits(StrictSpec):
    max_node_calls: StrictInt = Field(default=60, ge=1, le=120)
    max_llm_calls: StrictInt = Field(default=80, ge=1, le=160)
    max_reserved_tokens: StrictInt = Field(default=1000000, ge=1, le=4000000)
    timeout_seconds: StrictInt = Field(default=600, ge=1, le=1800)
    node_timeout_seconds: StrictInt = Field(default=120, ge=1, le=180)


class ProtocolRef(StrictSpec):
    protocol_id: str = Field(min_length=1, max_length=100)
    version: str | None = Field(default=None, pattern=r'^v[1-9][0-9]*$')
    revision: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def selection(self):
        if (self.version is None) == (self.revision is None):
            raise ValueError('请选择一个协议发布版本或已保存修订。')
        return self


class ProtocolSpec(StrictSpec):
    language_version: Literal['1'] = '1'
    family: Literal['pipeline', 'lead', 'debate']
    slots: dict[str, Slot] = Field(min_length=1, max_length=5)
    entry_stage: str
    stages: list[Stage] = Field(min_length=2, max_length=12)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode='after')
    def validate_protocol(self):
        import re
        stages = {s.stage_id: s for s in self.stages}
        if len(stages) != len(self.stages) or 'END' in stages or self.entry_stage not in stages:
            raise ValueError('阶段 ID 必须唯一，入口必须存在，END 为保留标识。')
        if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', k) for k in self.slots):
            raise ValueError('角色槽位 ID 无效。')
        if sum(slot.minimum for slot in self.slots.values()) > 5 or {s.slot for s in self.stages} != self.slots.keys():
            raise ValueError('协议需要的最少实例不能超过五个，且每个角色槽位必须被阶段使用。')
        for s in self.stages:
            if s.slot not in self.slots or self.slots[s.slot].role != CAPABILITIES[s.kind]:
                raise ValueError(f'{s.stage_id}：阶段与角色能力不匹配。')
            if not set(s.reads) <= stages.keys() or len(set(s.reads)) != len(s.reads):
                raise ValueError(f'{s.stage_id}：读取阶段不存在或重复。')
            if s.kind in ('plan', 'retrieve', 'review', 'lead') and s.mode != 'sequential':
                raise ValueError('单实例阶段必须使用顺序执行。')
            if s.kind == 'propose' and any(stages[k].kind not in ('plan', 'retrieve') for k in s.reads):
                raise ValueError('初始提案不得读取其他专家观点。')
            if s.on_limit not in stages.keys() | {'END'}:
                raise ValueError('超限出口不存在。')
            if s.on_limit != 'END' and (stages[s.on_limit].kind != 'review' or stages[s.on_limit].on_limit != 'END'):
                raise ValueError('超限只能结束或进入最终审核阶段。')
            conditions = [r.condition for r in s.routes]
            if len(set(conditions)) != len(conditions):
                raise ValueError('条件规则不能重复。')
            if len(conditions) == 1:
                if conditions != ['always']:
                    raise ValueError('单一出口必须使用 always。')
            elif 'always' in conditions or conditions.count('fallback') != 1:
                raise ValueError('条件路由必须有唯一 fallback，且不能混用 always。')
            allowed = {'always', 'fallback'} | ({'verified', 'unverified'} if s.kind == 'review' else
                {'lead_dispatch', 'lead_review'} if s.kind == 'lead' else set())
            if not set(conditions) <= allowed:
                raise ValueError('该阶段不支持所选路由规则。')
            if any(r.target not in stages.keys() | {'END'} for r in s.routes):
                raise ValueError('路由目标不存在。')
        reachable = set()
        def visit(key):
            if key in reachable: return
            reachable.add(key)
            if key != 'END':
                for r in stages[key].routes: visit(r.target)
        visit(self.entry_stage)
        if not stages.keys() <= reachable or 'END' not in reachable:
            raise ValueError('协议存在不可达阶段或缺少结束出口。')
        # Every branch must have a route to END, not just the entry branch.
        ending = {'END'}
        while True:
            expanded = ending | {s.stage_id for s in self.stages if any(r.target in ending for r in s.routes)}
            if expanded == ending: break
            ending = expanded
        if not stages.keys() <= ending:
            raise ValueError('存在无法结束的分支。')
        kinds = {s.kind for s in self.stages}
        for s in self.stages:
            read_kinds = {stages[k].kind for k in s.reads}
            needed = ({'retrieve': 'plan', 'review': 'retrieve'} if self.family == 'pipeline' else
                      {'lead': 'dispatch', 'dispatch': 'lead', 'review': 'dispatch'} if self.family == 'lead' else {})
            if s.kind in needed and needed[s.kind] not in read_kinds:
                raise ValueError(f'{s.stage_id}：缺少必需的阶段输入 {needed[s.kind]}。')
        required = {'pipeline': {'plan', 'retrieve', 'review'}, 'lead': {'lead', 'dispatch', 'review'},
                    'debate': {'propose', 'critique', 'revise', 'review'}}[self.family]
        if not required <= kinds:
            raise ValueError(f'{self.family} 缺少必需阶段：{sorted(required - kinds)}')
        if self.family == 'pipeline' and not kinds <= {'plan', 'retrieve', 'review'}:
            raise ValueError('Pipeline 仅支持规划、检索与审核。')
        if self.family == 'debate':
            experts = {s.slot for s in self.stages if s.kind in ('propose', 'critique', 'revise')}
            if len(experts) != 1 or self.slots[next(iter(experts))].minimum < 2:
                raise ValueError('Debate 的提案、质疑和修订必须使用同一组至少两位专家。')
            for s in self.stages:
                read_kinds = {stages[k].kind for k in s.reads}
                if s.kind == 'critique' and not read_kinds & {'propose', 'revise'}:
                    raise ValueError('质疑必须读取专家观点。')
                if s.kind == 'revise' and 'critique' not in read_kinds:
                    raise ValueError('修订必须读取质疑。')
                if s.kind == 'revise' and not read_kinds & {'propose', 'revise'}:
                    raise ValueError('修订必须读取本人的历史观点。')
                if s.kind == 'review' and 'revise' not in read_kinds:
                    raise ValueError('Debate 审核必须读取修订。')
        if self.family in ('pipeline', 'debate'):
            prerequisites = {'pipeline': {'retrieve': {'plan'}, 'review': {'retrieve'}},
                             'debate': {'critique': {'propose'}, 'revise': {'critique'}, 'review': {'revise'}}}[self.family]
            visited = set()
            def check_path(key, seen):
                identity = (key, frozenset(seen))
                if key == 'END' or identity in visited: return
                visited.add(identity)
                stage = stages[key]
                seen_kinds = {stages[k].kind for k in seen}
                if not prerequisites.get(stage.kind, set()) <= seen_kinds:
                    raise ValueError(f'{key}：存在绕过必需前序阶段的执行路径。')
                for required_kind in prerequisites.get(stage.kind, set()):
                    compatible = {'propose', 'revise'} if stage.kind == 'critique' else {required_kind}
                    if not any(k in seen and stages[k].kind in compatible for k in stage.reads):
                        raise ValueError(f'{key}：所读必需阶段在某条路径上尚未完成。')
                for route in stage.routes: check_path(route.target, seen | {key})
            check_path(self.entry_stage, set())
        return self


def protocol_templates_v1():
    def stage(key, kind, slot, target, **kw):
        return dict(stage_id=key, kind=kind, slot=slot, routes=[dict(target=target)], **kw)
    pipeline = dict(family='pipeline', slots={k: dict(role=r) for k, r in
        [('planner', 'planner'), ('retriever', 'retriever'), ('reviewer', 'answer_validator')]}, entry_stage='plan',
        stages=[stage('plan', 'plan', 'planner', 'retrieve'),
                stage('retrieve', 'retrieve', 'retriever', 'review', reads=['plan']),
                stage('review', 'review', 'reviewer', 'END', reads=['plan', 'retrieve'])])
    lead = dict(family='lead', slots={'lead': dict(role='lead'), 'experts': dict(role='expert', maximum=3),
        'reviewer': dict(role='answer_validator')}, entry_stage='coordinate', stages=[
        dict(stage_id='coordinate', kind='lead', slot='lead', reads=['work'], max_visits=3, on_limit='review', routes=[
            dict(condition='lead_dispatch', target='work'), dict(condition='lead_review', target='review'),
            dict(condition='fallback', target='review')]),
        stage('work', 'dispatch', 'experts', 'coordinate', mode='parallel', reads=['coordinate'], max_visits=2, on_limit='review'),
        stage('review', 'review', 'reviewer', 'END', reads=['work', 'coordinate'])])
    debate = dict(family='debate', slots={'experts': dict(role='expert', minimum=2, maximum=3),
        'reviewer': dict(role='answer_validator')}, entry_stage='initial', stages=[
        stage('initial', 'propose', 'experts', 'critique', mode='parallel'),
        stage('critique', 'critique', 'experts', 'revise', mode='parallel', reads=['initial', 'revise'], max_visits=2, on_limit='review'),
        stage('revise', 'revise', 'experts', 'review', mode='parallel', reads=['initial', 'critique', 'revise'], max_visits=2, on_limit='review'),
        dict(stage_id='review', kind='review', slot='reviewer', reads=['revise'], max_visits=2, routes=[
            dict(condition='verified', target='END'), dict(condition='unverified', target='critique'),
            dict(condition='fallback', target='END')])])
    return [{'template_id': p['family'], 'name': name, 'spec': ProtocolSpec.model_validate(p).model_dump()}
            for p, name in [(pipeline, 'Pipeline 流水线'), (lead, 'Lead Agent 主导分派'), (debate, 'Debate 专家辩论')]]


def compile_protocol(raw, bindings, agents):
    spec = parse_protocol(raw)
    by_id = {a['agent_id']: a for a in agents}
    if set(bindings) != set(spec.slots):
        raise ValueError('请完整绑定协议角色槽位，不能包含未知槽位。')
    used = []
    for key, slot in spec.slots.items():
        ids = bindings[key]
        if not isinstance(ids, list) or not slot.minimum <= len(ids) <= slot.maximum or len(ids) != len(set(ids)):
            raise ValueError(f'{key}：绑定人数无效。')
        if any(i not in by_id or by_id[i]['agent_type'] != slot.role for i in ids):
            raise ValueError(f'{key}：绑定的智能体不存在或类型不匹配。')
        used.extend(ids)
    if len(used) != len(set(used)) or set(used) != set(by_id):
        raise ValueError('每个实例需绑定且只能绑定一个角色槽位；同一槽位可跨阶段重复使用。')
    return {'compiler_version': spec.language_version, 'spec': spec.model_dump(), 'bindings': deepcopy(bindings)}


def workflow_metadata(document):
    spec = parse_protocol(document['spec'])
    return dict(protocol_id=document['protocol_id'], method_id=f'configurable_{spec.family}_v{spec.language_version}',
        entry_node=spec.entry_stage, agents=[s.stage_id for s in spec.stages], max_cycles={},
        edges=[{'from': s.stage_id, 'to': r.target, 'route_name': r.condition} for s in spec.stages for r in s.routes])


def parse_protocol(raw):
    if raw.get('language_version', '1') == '2':
        from src.runtime.protocols_v2 import ProtocolSpecV2
        return ProtocolSpecV2.model_validate(raw)
    return ProtocolSpec.model_validate(raw)


def protocol_templates():
    from src.runtime.protocols_v2 import templates_v2
    return templates_v2()
