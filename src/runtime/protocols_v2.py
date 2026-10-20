"""Additive V2 language; V1 normalization and compilation remain frozen."""
from copy import deepcopy
import re
from typing import Literal
from pydantic import Field, StrictInt, model_validator
from src.runtime.specs import StrictSpec
from src.runtime.protocols import Slot, Limits, Stage, CAPABILITIES, ProtocolSpec, protocol_templates_v1

KINDS = [*CAPABILITIES, 'answer']
CONDITIONS = ['always', 'verified', 'unverified', 'lead_dispatch', 'lead_review', 'fallback', 'insufficient_retry', 'conflict_retry']


class TransitionV2(StrictSpec):
    condition: Literal['always', 'verified', 'unverified', 'lead_dispatch', 'lead_review', 'fallback', 'insufficient_retry', 'conflict_retry'] = 'always'
    target: str = Field(pattern=r'^(END|[A-Za-z][A-Za-z0-9_-]{0,63})$')


class StageV2(Stage):
    kind: Literal['plan', 'retrieve', 'propose', 'critique', 'revise', 'lead', 'dispatch', 'review', 'answer']
    routes: list[TransitionV2] = Field(min_length=1, max_length=4)
    max_visits: StrictInt = Field(default=1, ge=1, le=12)


class RetryLimits(StrictSpec):
    retrieval: StrictInt = Field(default=3, ge=0, le=5)
    planning: StrictInt = Field(default=2, ge=0, le=5)


class ProtocolSpecV2(StrictSpec):
    language_version: Literal['2'] = '2'
    family: Literal['pipeline', 'single_expert', 'multi_expert', 'lead', 'debate']
    slots: dict[str, Slot] = Field(min_length=1, max_length=5)
    entry_stage: str
    stages: list[StageV2] = Field(min_length=1, max_length=12)
    limits: Limits = Field(default_factory=Limits)
    retry_limits: RetryLimits = Field(default_factory=RetryLimits)

    @model_validator(mode='after')
    def graph_contract(self):
        stages = {s.stage_id: s for s in self.stages}
        capabilities = {**CAPABILITIES, 'answer': 'expert'}
        if len(stages) != len(self.stages) or 'END' in stages or self.entry_stage not in stages:
            raise ValueError('阶段 ID 必须唯一，入口必须存在，END 为保留标识。')
        if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', key) for key in self.slots):
            raise ValueError('角色槽位 ID 无效。')
        if {s.slot for s in self.stages} != self.slots.keys() or sum(s.minimum for s in self.slots.values()) > 5:
            raise ValueError('槽位必须被使用，协议的最少实例总数不能超过五个。')
        for s in self.stages:
            if s.slot not in self.slots or self.slots[s.slot].role != capabilities[s.kind]:
                raise ValueError(f'{s.stage_id}：阶段与角色能力不匹配。')
            if not set(s.reads) <= stages.keys() or len(set(s.reads)) != len(s.reads):
                raise ValueError('读取阶段不存在或重复。')
            if s.kind in ('plan', 'retrieve', 'lead', 'review', 'answer') and s.mode != 'sequential':
                raise ValueError('单实例阶段必须顺序执行。')
            if s.kind == 'propose' and any(stages[k].kind not in ('plan', 'retrieve') for k in s.reads):
                raise ValueError('初始观点不得读取其他专家观点。')
            if s.on_limit not in stages.keys() | {'END'}:
                raise ValueError('超限出口不存在。')
            if s.on_limit != 'END' and (stages[s.on_limit].kind != 'review' or stages[s.on_limit].on_limit != 'END'):
                raise ValueError('超限只能结束或进入最终审核。')
            names = [r.condition for r in s.routes]
            if len(names) != len(set(names)) or (len(names) == 1 and names != ['always']):
                raise ValueError('单出口必须为 always，条件不能重复。')
            if len(names) > 1 and ('always' in names or names.count('fallback') != 1):
                raise ValueError('条件路由需要唯一 fallback，不能混用 always。')
            allowed = {'always', 'fallback'}
            if s.kind == 'lead': allowed |= {'lead_dispatch', 'lead_review'}
            if s.kind == 'review':
                allowed |= {'verified', 'unverified'}
                if self.family == 'pipeline': allowed |= {'insufficient_retry', 'conflict_retry'}
            if not set(names) <= allowed: raise ValueError('阶段不支持所选路由规则。')
            if any(r.target not in stages.keys() | {'END'} for r in s.routes): raise ValueError('路由目标不存在。')
            for r in s.routes:
                if r.target == 'END' and s.kind not in ('review', 'answer'):
                    raise ValueError('只有最终作答或审核阶段可以正常结束。')
                if r.condition in ('insufficient_retry', 'conflict_retry'):
                    wanted = 'retrieve' if r.condition == 'insufficient_retry' else 'plan'
                    if r.target == 'END' or stages[r.target].kind != wanted:
                        raise ValueError('反馈重试必须返回对应的检索或规划阶段。')
        if self.family in ('lead', 'debate'):
            raw = self.model_dump(exclude={'retry_limits'})
            raw['language_version'] = '1'
            for s in raw['stages']: s['max_visits'] = min(s['max_visits'], 5)
            ProtocolSpec.model_validate(raw)
        else:
            kinds = [s.kind for s in self.stages]
            if self.family == 'single_expert':
                if kinds != ['answer'] or len(self.slots) != 1 or next(iter(self.slots.values())).maximum != 1:
                    raise ValueError('单专家需要唯一 Expert 作答阶段和一个实例。')
            if self.family == 'multi_expert':
                if kinds.count('plan') > 1 or kinds.count('propose') != 1 or kinds.count('review') != 1 or not set(kinds) <= {'plan', 'propose', 'review'}:
                    raise ValueError('多专家汇总需要一个专家阶段、一个审核阶段和至多一个规划阶段。')
                if len({s.slot for s in self.stages}) != len(self.stages): raise ValueError('多专家阶段需使用各自角色槽位。')
            if self.family == 'pipeline' and (not {'plan', 'retrieve', 'review'} <= set(kinds) or not set(kinds) <= {'plan', 'retrieve', 'review'}):
                raise ValueError('流水线需要规划、检索、审核阶段。')
            prerequisites = {'pipeline': {'retrieve': {'plan'}, 'review': {'retrieve'}},
                             'multi_expert': {'review': {'propose'}, **({'propose': {'plan'}} if 'plan' in kinds else {})},
                             'single_expert': {}}[self.family]
            seen_states, reachable = set(), set()
            def walk(key, prior):
                reachable.add(key)
                if key == 'END' or (key, frozenset(prior)) in seen_states: return
                seen_states.add((key, frozenset(prior)))
                stage = stages[key]
                for needed in prerequisites.get(stage.kind, set()):
                    if not any(k in prior and stages[k].kind == needed for k in stage.reads):
                        raise ValueError(f'{key} 缺少已完成的 {needed} 阶段输入。')
                for route in stage.routes: walk(route.target, prior | {key})
            walk(self.entry_stage, set())
            if not stages.keys() <= reachable or 'END' not in reachable: raise ValueError('存在不可达阶段或缺少结束出口。')
            ending = {'END'}
            while True:
                expanded = ending | {s.stage_id for s in self.stages if any(r.target in ending for r in s.routes)}
                if expanded == ending: break
                ending = expanded
            if not stages.keys() <= ending: raise ValueError('存在无法结束的分支。')
        return self


def templates_v2():
    old = {t['template_id']: deepcopy(t) for t in protocol_templates_v1()}
    for t in old.values(): t['spec']['language_version'] = '2'
    fixed = old['pipeline']; fixed['name'] = '固定流水线'
    feedback = deepcopy(fixed); feedback.update(template_id='pipeline_feedback', name='反馈重试流水线')
    p, r, v = feedback['spec']['stages']
    p.update(reads=['review'], max_visits=3)
    r.update(reads=['plan', 'review'], max_visits=6)
    v.update(max_visits=6, routes=[dict(condition='verified', target='END'),
        dict(condition='insufficient_retry', target='retrieve'), dict(condition='conflict_retry', target='plan'), dict(condition='fallback', target='END')])
    solo = dict(template_id='single_expert', name='单专家问答', spec=dict(language_version='2', family='single_expert',
        slots={'expert': dict(role='expert')}, entry_stage='answer', stages=[dict(stage_id='answer', kind='answer', slot='expert', routes=[dict(target='END')])]))
    team = dict(template_id='multi_expert', name='多专家汇总', spec=dict(language_version='2', family='multi_expert',
        slots={'planner': dict(role='planner'), 'experts': dict(role='expert', maximum=3), 'reviewer': dict(role='answer_validator')},
        entry_stage='plan', stages=[dict(stage_id='plan', kind='plan', slot='planner', routes=[dict(target='opinions')]),
            dict(stage_id='opinions', kind='propose', slot='experts', reads=['plan'], routes=[dict(target='review')]),
            dict(stage_id='review', kind='review', slot='reviewer', reads=['plan', 'opinions'], routes=[dict(target='END')])]))
    results = [fixed, feedback, solo, team, old['lead'], old['debate']]
    for t in results:
        t['spec'] = ProtocolSpecV2.model_validate(t['spec']).model_dump()
        t['description'] = {'pipeline': '规划、检索、审核，一次审核后结束。', 'pipeline_feedback': '证据不足补检索，证据冲突重规划。',
            'single_expert': '一个专家独立检索、作答并校验引用。', 'multi_expert': '独立专家串行或并行作答，汇合后统一审核。',
            'lead': '主导者选择专家并分派具体任务，汇总后审核。', 'debate': '独立提案、质疑、修订与裁决。'}[t['template_id']]
    return results


def language_capabilities():
    v1_kinds, v1_conditions = list(CAPABILITIES), CONDITIONS[:6]
    def entry(kinds, conditions, minimum, maximum):
        return dict(stage_kinds=kinds, conditions=conditions, roles=['planner', 'retriever', 'expert', 'answer_validator', 'lead'],
            min_stages=minimum, max_stages=12, max_visits=maximum, max_routes=4, max_slot_size=3,
            max_retry_count=5, limit_target_kinds=['review'],
            family_kinds=({'pipeline': ['plan', 'retrieve', 'review'], 'single_expert': ['answer'],
                'multi_expert': ['plan', 'propose', 'review'], 'lead': v1_kinds, 'debate': v1_kinds} if 'answer' in kinds else {}),
            family_min_stages={'pipeline': 3, 'single_expert': 1, 'multi_expert': 2, 'lead': 3, 'debate': 4},
            parallel_kinds=['propose', 'dispatch', 'critique', 'revise'], final_kinds=['review'] + (['answer'] if 'answer' in kinds else []),
            conditions_by_kind={kind: ['always', 'fallback'] + (['lead_dispatch', 'lead_review'] if kind == 'lead' else
                ['verified', 'unverified'] + (['insufficient_retry', 'conflict_retry'] if 'insufficient_retry' in conditions else []) if kind == 'review' else []) for kind in kinds})
    return {'1': entry(v1_kinds, v1_conditions, 2, 5), '2': entry(KINDS, CONDITIONS, 1, 12)}
