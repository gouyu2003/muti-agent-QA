"""Registered instance node factories. No user supplied executable code."""
from copy import deepcopy
from functools import partial
import hashlib
import json

from src.agents.planner import planner_fn
from src.agents.retriever import retriever_fn
from src.agents.answer_validator import answer_validator_fn
from src.runtime.system import retriever_from_snapshot


def evidence_ids(groups: list[dict]) -> list[dict]:
    groups = deepcopy(groups)
    for group in groups:
        for chunk in group['chunks']:
            identity = [group['sq_id']] + [chunk.get(field) for field in
                ('source_id', 'source_version', 'chunk_id', 'source_title', 'source_paragraph', 'text')]
            chunk['evidence_id'] = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return groups


def expert_input(state):
    return {'question': state['question'], 'sub_questions': deepcopy(state.get('sub_questions') or [
        {'id': 'sq_1', 'question': state['question'], 'keywords': [], 'depends_on': []}])}


def expert_node(state, *, agent_id, config, retriever, usage, solo):
    # Never expose another expert's answer, even in sequential mode.
    local = expert_input(state)
    retrieval = retriever_fn(local, retriever=retriever, agent_config=config)
    local['evidence_chunks'] = evidence_ids(retrieval['evidence_chunks'])
    result = answer_validator_fn(local, agent_config=config, usage_collector=usage, expert_mode=not solo)
    result['evidence_chunks'] = local['evidence_chunks']
    result['agent_name'] = config['agent_name']
    update = {'expert_results': {agent_id: result}}
    if solo:
        update.update(result)
        update['sub_questions'] = local['sub_questions']
    return update


def review_node(state, *, config, usage, expert_ids):
    results = state.get('expert_results', {})
    if set(results) != set(expert_ids):
        raise ValueError('审核开始时专家结果不完整。')
    local = expert_input(state)
    local['expert_results'] = {key: results[key] for key in expert_ids}
    groups: dict[str, dict] = {}
    for key in expert_ids:
        for group in results[key]['evidence_chunks']:
            merged = groups.setdefault(group['sq_id'], {**group, 'chunks': []})
            known = {c['evidence_id'] for c in merged['chunks']}
            merged['chunks'].extend(c for c in group['chunks'] if c['evidence_id'] not in known)
    for group in groups.values():
        if group['chunks']:
            group['sufficiency'] = 'unknown'
            group['insufficiency_note'] = ''
    local['evidence_chunks'] = list(groups.values())
    result = answer_validator_fn(local, agent_config=config, usage_collector=usage)
    return {**result, 'evidence_chunks': local['evidence_chunks'], 'sub_questions': local['sub_questions']}


def _planner(config, source, usage, snapshot, key):
    return partial(planner_fn, agent_config=config, usage_collector=usage)


def _retriever(config, source, usage, snapshot, key):
    return partial(retriever_fn, agent_config=config, retriever=retriever_from_snapshot({'source': source}))


def _validator(config, source, usage, snapshot, key):
    if snapshot['workflow_spec']['protocol_id'] == 'multi_expert':
        return partial(review_node, config=config, usage=usage,
                       expert_ids=[i for i, t in snapshot['agent_types'].items() if t == 'expert'])
    return partial(answer_validator_fn, agent_config=config, usage_collector=usage)


def _expert(config, source, usage, snapshot, key):
    return partial(expert_node, agent_id=key, config=config, usage=usage,
        retriever=retriever_from_snapshot({'source': source}),
        solo=snapshot['workflow_spec']['protocol_id'] == 'single_expert')


AGENT_REGISTRY = {'planner': _planner, 'retriever': _retriever,
                  'answer_validator': _validator, 'expert': _expert}


def instance_nodes(snapshot, usage):
    result = {}
    for key, config in snapshot['agents'].items():
        role = snapshot['agent_types'][key]
        if role not in AGENT_REGISTRY:
            raise ValueError(f'未注册的智能体类型：{role}')
        child = usage.for_agent(key)
        impl = AGENT_REGISTRY[role](config, snapshot.get('sources', {}).get(key), child, snapshot, key)
        def node(state, impl=impl, key=key):
            with usage.running_node():
                output = impl(state)
                usage.record_output(key, output)
                return output
        result[key] = node
    return result
