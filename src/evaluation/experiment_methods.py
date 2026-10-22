"""Compile the six existing protocols without touching application stores."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3

from src.evaluation.experiment_config import digest, atomic_json
from src.evaluation.hotpotqa import sha256_file
from src.knowledge.hotpotqa_context import runtime_input
from src.runtime.instances import templates, validate_instances, agent_config
from src.runtime.models import RunContext
from src.runtime.protocols import protocol_templates, compile_protocol, parse_protocol, workflow_metadata
from src.runtime.system_store import fingerprint

TEMPLATES = {'single_rag': 'single_expert', 'pipeline_fixed': 'pipeline',
             'pipeline_adaptive': 'pipeline_feedback', 'multi_expert': 'multi_expert',
             'lead': 'lead', 'debate': 'debate'}
ADAPTER_VERSION = 'hotpotqa-context-v1'


def method_definition(config, method):
    source = {'kind': 'builtin', 'template': TEMPLATES[method]}
    if method in config.published_systems:
        ref = config.published_systems[method]
        # mode=ro never initializes/migrates the user's system database.
        with sqlite3.connect(Path(ref.database).resolve().as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT document FROM releases WHERE system_id=? AND version=?',
                             (ref.system_id, int(ref.version[1:]))).fetchone()
        if row is None:
            raise ValueError('Published system version not found')
        import json
        original = json.loads(row[0])['snapshot']
        if original.get('schema_version') != '3' or fingerprint(original) != original['config_sha256']:
            raise ValueError('Published method requires a valid schema-3 snapshot')
        spec = deepcopy(original['protocol_document']['spec'])
        instances = deepcopy(original['instances'])
        bindings = deepcopy(original['protocol_bindings'])
        configs = deepcopy(original['agents'])
        source = {'kind': 'published_system', 'system_id': ref.system_id, 'version': ref.version,
                  'original_sha256': original['config_sha256'], 'material_transform': ADAPTER_VERSION}
    else:
        spec = deepcopy(next(t['spec'] for t in protocol_templates() if t['template_id'] == TEMPLATES[method]))
        defaults = {t['agent_type']: t['defaults'] for t in templates()}
        instances, bindings = [], {}
        for slot, contract in spec['slots'].items():
            count = config.expert_count if contract['role'] == 'expert' and method != 'single_rag' else 1
            bindings[slot] = []
            for i in range(count):
                instance = deepcopy(defaults[contract['role']])
                instance.update(agent_id=f'{slot}_{i}', name=f'{slot}_{i}')
                bindings[slot].append(instance['agent_id'])
                instances.append(instance)
        configs = {}

    if spec.get('language_version') != '2':
        raise ValueError('Experiments require language V2; no implicit protocol migration')
    expected_family = 'pipeline' if method.startswith('pipeline_') else TEMPLATES[method]
    if spec['family'] != expected_family:
        raise ValueError('Published protocol family does not match experiment method')
    # Exact template structure for baseline names, to avoid mislabeled custom methods.
    template = next(t['spec'] for t in protocol_templates() if t['template_id'] == TEMPLATES[method])
    shape = lambda s: [(x['stage_id'], x['kind'], x['slot'], x['reads'], x['routes']) for x in s['stages']]
    if shape(spec) != shape(template) or spec['slots'] != template['slots']:
        raise ValueError('Custom graph cannot use a standard baseline name; use the corresponding template')
    spec['limits'] = config.limits.model_dump()
    stages = {s['kind']: s for s in spec['stages']}
    for stage in spec['stages']:
        if stage['kind'] in ('propose', 'dispatch', 'critique', 'revise'):
            stage['mode'] = config.execution_mode
    if method == 'pipeline_adaptive':
        spec['retry_limits'] = {'retrieval': config.retrieval_retries, 'planning': config.plan_retries}
        stages['plan']['max_visits'] = config.plan_retries + 1
        stages['retrieve']['max_visits'] = stages['review']['max_visits'] = 1 + config.retrieval_retries + config.plan_retries
    if method == 'lead':
        stages['lead']['max_visits'] = config.lead_rounds + 1
        stages['dispatch']['max_visits'] = config.lead_rounds
    if method == 'debate':
        for kind in ('critique', 'revise', 'review'):
            stages[kind]['max_visits'] = config.debate_rounds
    for instance in instances:
        role = instance['agent_type']
        instance['top_k'] = config.top_k
        if role in ('expert', 'retriever'):
            instance.update(source_id='experiment_context', source_version=None)
        if instance.get('model_config'):
            instance['model_config'].update(model_name=config.model.name, temperature=config.model.temperature,
                                            max_tokens=config.model.max_tokens)
    agents = validate_instances(instances)
    if method in ('multi_expert', 'lead', 'debate') and sum(a.agent_type == 'expert' for a in agents) != config.expert_count:
        raise ValueError('Published expert count does not match experiment configuration')
    for agent in agents:
        configs[agent.agent_id] = agent_config(agent, configs.get(agent.agent_id))
        configs[agent.agent_id]['failure_handling'] = {'max_retries': config.model.output_retries}
        # All knowledge is supplied by the per-question adapter, never original materials.
        configs[agent.agent_id]['knowledge_sources'] = []
    spec = parse_protocol(spec).model_dump()
    document = dict(protocol_id=f'experiment_{method}', name=method, version='v1', spec=spec)
    workflow = workflow_metadata(document)
    workflow['method_id'] = method
    snapshot = dict(schema_version='3', system_id=f'experiment_{method}', system_version='v1',
                    instances=[a.model_dump(by_alias=True) for a in agents], agents=configs,
                    agent_types={a.agent_id: a.agent_type for a in agents}, sources={},
                    workflow_spec=workflow, protocol_document=document,
                    protocol_bindings=bindings, protocol_plan=compile_protocol(spec, bindings, instances),
                    protocol={'protocol_name': method}, execution_mode=config.execution_mode,
                    configuration_errors=[])
    result = {'method_id': method, 'adapter_version': ADAPTER_VERSION, 'source': source, 'snapshot': snapshot}
    result['sha256'] = digest(result)
    return result


def build_context(definition, record, directory, experiment_id, run_id, code_id):
    public = runtime_input(record)
    path = Path(directory) / 'contexts' / (digest(public['_id']) + '.json')
    atomic_json(path, public)
    snapshot = deepcopy(definition['snapshot'])
    source_id = 'sample_' + digest(public['_id'])[:24]
    source = dict(source_id=source_id, source_type='hotpotqa_context', resolved_path=str(path.resolve()),
                  sha256=sha256_file(path), name='Isolated HotpotQA context')
    for instance in snapshot['instances']:
        if instance['agent_type'] in ('expert', 'retriever'):
            instance.update(source_id=source_id, source_version=None)
            snapshot['sources'][instance['agent_id']] = deepcopy(source)
            snapshot['agents'][instance['agent_id']]['knowledge_sources'] = [
                dict(source_id=source_id, source_type='hotpotqa_context')]
    snapshot['source'] = source
    snapshot['protocol_plan'] = compile_protocol(snapshot['protocol_document']['spec'],
                                                snapshot['protocol_bindings'], snapshot['instances'])
    snapshot['experiment_derivation'] = dict(method_sha256=definition['sha256'],
                                              adapter_version=ADAPTER_VERSION, origin=definition['source'])
    snapshot['config_sha256'] = fingerprint(snapshot)
    return RunContext(run_id=run_id, entrypoint='batch', question=public['question'],
                      source_id=source_id, record_id=public['_id'], sample_id=public['_id'],
                      experiment_id=experiment_id, workflow_id=snapshot['workflow_spec']['protocol_id'],
                      protocol_id=snapshot['workflow_spec']['protocol_id'], method_id=definition['method_id'],
                      model_name=next(c['model_config']['model_name'] for c in snapshot['agents'].values()
                                      if c.get('model_config')), code_version=code_id, config_snapshot=snapshot)
