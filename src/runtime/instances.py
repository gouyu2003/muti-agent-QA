"""Server-owned role contracts and compilation of saved agent instances."""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import Field, StrictInt, field_validator

from src.config_loader import load_agent_configs
from src.runtime.specs import StrictSpec, WorkflowSpec

AgentType = Literal['planner', 'retriever', 'answer_validator', 'expert']
ExecutionMode = Literal['sequential', 'parallel']
SOLO = 'single_expert'
TEAM = 'multi_expert'
TEMPLATE_NAMES = {SOLO: '单专家问答', TEAM: '多专家汇总',
                  'multi_hop_qa_fixed': '固定流水线', 'multi_hop_qa_v0': '反馈重试流水线'}


class ModelSettings(StrictSpec):
    model_name: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(min_length=1, max_length=30000)
    temperature: float = Field(default=0, ge=0, le=2, strict=True)
    max_tokens: StrictInt = Field(default=4096, ge=1, le=65536)

    @field_validator('model_name', 'system_prompt')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('模型名称或提示词不能为空。')
        return value


class AgentInstance(StrictSpec):
    agent_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    agent_type: AgentType
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    model_config_value: ModelSettings | None = Field(default=None, alias='model_config')
    source_id: str | None = Field(default=None, min_length=1)
    source_version: StrictInt | None = Field(default=None, ge=1)
    top_k: StrictInt = Field(default=3, ge=1, le=50)


def templates() -> list[dict]:
    defaults = load_agent_configs()
    result = []
    for role in ('planner', 'retriever', 'answer_validator', 'expert'):
        base = defaults['answer_validator_v0' if role == 'expert' else role + '_v0']
        model = deepcopy(base['model_config']) if role != 'retriever' else None
        if role == 'expert':
            model['system_prompt'] = ('You are an independent evidence-grounded domain expert. '
                'Answer the original question using only the supplied local evidence. '
                'Report missing evidence or unresolved conflicts honestly.')
        instance = AgentInstance(agent_id=role + '_1', agent_type=role,
            name='领域专家' if role == 'expert' else base['agent_name'],
            description='独立检索本实例材料并提出有证据的答案。' if role == 'expert' else base.get('description', ''),
            model_config=model)
        result.append({'agent_type': role, 'name': instance.name,
            'uses_llm': role != 'retriever', 'uses_knowledge': role in ('retriever', 'expert'),
            'editable_fields': ['name', 'description'] + (['model_config'] if model else [])
                + (['source_id', 'source_version', 'top_k'] if role in ('retriever', 'expert') else []),
            'input_fields': ['question', 'sub_questions'] if role in ('retriever', 'expert') else
                (['question', 'expert_results', 'evidence_chunks'] if role == 'answer_validator' else ['question']),
            'output_fields': {'planner': ['sub_questions', 'retrieval_strategy'],
                'retriever': ['evidence_chunks'], 'expert': ['expert_results'],
                'answer_validator': ['answer', 'verified', 'supporting_evidence']}[role],
            'defaults': instance.model_dump(by_alias=True)})
    return result


def legacy_instances(snapshot: dict) -> list[dict]:
    if snapshot.get('instances') is not None:
        return deepcopy(snapshot['instances'])
    source = snapshot.get('source', {})
    return [AgentInstance(agent_id=key, agent_type=config['role'], name=config['agent_name'],
        description=config.get('description', ''), model_config=config['model_config'] if config['role'] != 'retriever' else None,
        source_id=source.get('source_id') if config['role'] == 'retriever' else None,
        source_version=source.get('version') if config['role'] == 'retriever' else None,
        top_k=config.get('retrieval_config', {}).get('top_k', 3)).model_dump(by_alias=True)
        for key, config in snapshot['agents'].items()]


def validate_instances(raw: list[dict], previous: list[dict] | None = None) -> list[AgentInstance]:
    if not 1 <= len(raw) <= 5:
        raise ValueError('每套系统需要 1～5 个智能体实例。')
    agents = [AgentInstance.model_validate(item) for item in raw]
    ids = [a.agent_id for a in agents]
    if len(set(ids)) != len(ids):
        raise ValueError('智能体实例 ID 不能重复。')
    old_types = {a['agent_id']: a['agent_type'] for a in previous or []}
    for a in agents:
        if not a.name.strip():
            raise ValueError(f'{a.agent_id}：名称不能为空。')
        if a.agent_id in old_types and old_types[a.agent_id] != a.agent_type:
            raise ValueError(f'{a.name}：实例类型不能修改，请删除后创建新实例。')
        if a.agent_type != 'retriever' and a.model_config_value is None:
            raise ValueError(f'{a.name}：需要模型与提示词配置。')
        if a.agent_type == 'retriever' and a.model_config_value is not None:
            raise ValueError(f'{a.name}：BM25 检索节点不使用模型参数。')
        if a.agent_type not in ('retriever', 'expert') and (a.source_id or a.source_version is not None):
            raise ValueError(f'{a.name}：此角色不直接绑定知识源。')
        if a.source_version is not None and not a.source_id:
            raise ValueError(f'{a.name}：知识源版本必须同时指定知识源。')
    return agents


def compile_workflow(template: str | None, agents: list[AgentInstance], mode: str) -> WorkflowSpec:
    roles = {role: [a.agent_id for a in agents if a.agent_type == role]
             for role in ('planner', 'retriever', 'answer_validator', 'expert')}
    p, r, v, e = (roles[k] for k in ('planner', 'retriever', 'answer_validator', 'expert'))
    edges = []
    def edge(a, b, route='always'):
        edges.append({'edge_id': f'e{len(edges)}', 'from': a, 'to': b, 'route_name': route})
    join_sources, join_target = [], None
    limits = {}
    if template in ('multi_hop_qa_fixed', 'multi_hop_qa_v0'):
        if [len(p), len(r), len(v), len(e)] != [1, 1, 1, 0]:
            raise ValueError('流水线需要各一个 Planner、Retriever、Validator。')
        if mode != 'sequential':
            raise ValueError('流水线仅支持顺序执行。')
        entry = p[0]
        edge(p[0], r[0]); edge(r[0], v[0])
        if template == 'multi_hop_qa_fixed':
            edge(v[0], 'END')
        else:
            for target, route in [('END', 'verified'), (r[0], 'insufficient_retry'),
                                  (p[0], 'conflict_retry'), ('END', 'fallback')]:
                edge(v[0], target, route)
            limits = {'retrieval_retry_count': 3, 'plan_retry_count': 2}
    elif template == SOLO:
        if len(agents) != 1 or len(e) != 1:
            raise ValueError('单专家模板需要且只能有一个 Expert。')
        entry = e[0]; edge(e[0], 'END')
    elif template == TEAM:
        if len(p) > 1 or r or len(v) != 1 or not 1 <= len(e) <= 3:
            raise ValueError('多专家模板需要 1～3 个 Expert、1 个 Validator，可选 1 个 Planner。')
        entry = p[0] if p else e[0]
        if mode == 'parallel' and len(e) > 1:
            # START fan-out is compiled separately if there is no planner.
            if p:
                for expert in e: edge(p[0], expert)
            join_sources, join_target = e, v[0]
            for expert in e: edge(expert, v[0])
        else:
            if p: edge(p[0], e[0])
            for a, b in zip(e, e[1:]): edge(a, b)
            edge(e[-1], v[0])
        edge(v[0], 'END')
    else:
        raise ValueError('请选择已注册的执行模板。')
    ordered = p + r + v if template in ('multi_hop_qa_fixed', 'multi_hop_qa_v0') else p + e + v
    return WorkflowSpec(protocol_id=template, method_id={SOLO: 'single_expert_v1', TEAM: 'multi_expert_v1',
        'multi_hop_qa_fixed': 'multi_agent_fixed_v1', 'multi_hop_qa_v0': 'multi_agent_adaptive_v1'}[template],
        agents=ordered, entry_node=entry, edges=edges, max_cycles=limits,
        join_sources=join_sources, join_target=join_target,
        start_nodes=e if template == TEAM and mode == 'parallel' and not p else [],
        conditional_nodes=v if limits else [])


def agent_config(instance: AgentInstance, base: dict | None = None) -> dict:
    config = deepcopy(base) if base else {}
    config.update(agent_id=instance.agent_id, role=instance.agent_type,
        agent_name=instance.name.strip(), description=instance.description,
        model_config=instance.model_config_value.model_dump() if instance.model_config_value else {},
        retrieval_config={'top_k': instance.top_k})
    config.setdefault('failure_handling', {'max_retries': 2})
    return config
