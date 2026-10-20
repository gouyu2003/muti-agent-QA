"""Compile constrained stages to LangGraph; preserve every invocation separately."""
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from copy import deepcopy
from contextvars import copy_context
from dataclasses import asdict
import json
from threading import RLock
from time import monotonic
from typing import Annotated, Any, NotRequired, TypedDict
from uuid import uuid4

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from src.agents.planner import planner_fn
from src.agents.retriever import retriever_fn
from src.agents.answer_validator import answer_validator_fn
from src.runtime.nodes import evidence_ids
from src.runtime.protocols import parse_protocol, compile_protocol
from src.runtime.system import retriever_from_snapshot
from src.state import merge_expert_results


class ProtocolExecutionError(RuntimeError):
    pass


class ProtocolState(TypedDict):
    question: str
    protocol_calls: NotRequired[Annotated[dict[str, Any], merge_expert_results]]
    stage_visits: NotRequired[dict[str, int]]
    next_stage: NotRequired[str]
    limit_reached: NotRequired[bool]
    retry_counters: NotRequired[dict[str, int]]


class ResourceGuard:
    def __init__(self, limits, cancelled):
        self.limits, self.cancelled = limits, cancelled
        self.started = monotonic()
        self.lock = RLock()
        self.nodes = self.llm_calls = self.reserved = 0

    def check(self):
        if self.cancelled.is_set(): raise ProtocolExecutionError('协议运行已停止。')
        if monotonic() - self.started >= self.limits.timeout_seconds:
            raise ProtocolExecutionError('协议运行超过时间上限。')

    def start_node(self):
        with self.lock:
            self.check()
            if self.nodes >= self.limits.max_node_calls: raise ProtocolExecutionError('协议节点调用预算已耗尽。')
            self.nodes += 1
            return self.nodes

    def reserve(self, prompt, *, max_tokens):
        with self.lock:
            self.check()
            # UTF-8 bytes plus requested output tokens are conservative reservations,
            # not claimed provider usage. Reservations include failed/empty retries.
            estimate = len(prompt.encode('utf-8')) + max_tokens
            if self.llm_calls >= self.limits.max_llm_calls: raise ProtocolExecutionError('协议模型调用预算已耗尽。')
            if self.reserved + estimate > self.limits.max_reserved_tokens: raise ProtocolExecutionError('协议 Token 预留预算已耗尽。')
            self.llm_calls += 1
            self.reserved += estimate

    def remaining(self):
        self.check()
        return max(.01, min(self.limits.node_timeout_seconds, self.limits.timeout_seconds - (monotonic() - self.started)))


def latest_reads(state, stage, version='1'):
    calls = list(state.get('protocol_calls', {}).values())
    chosen = []
    for key in stage.reads:
        candidates = [c for c in calls if c['stage_id'] == key and c['status'] == 'completed']
        if candidates:
            round_no = max(c['round'] for c in candidates)
            fields = ('call_id', 'agent_id', 'stage_id', 'round', 'kind', 'output') + (('invocation_index',) if version == '2' else ())
            chosen.extend({k: deepcopy(c[k]) for k in fields}
                          for c in candidates if c['round'] == round_no or c['kind'] == 'dispatch')
    return deepcopy(sorted(chosen, key=lambda c: c['invocation_index']) if version == '2' else chosen)


def local_state(question, reads, share_evidence=True):
    local = {'question': question, 'sub_questions': [{'id': 'sq_1', 'question': question, 'keywords': [], 'depends_on': []}]}
    groups = {}
    for c in reads:
        output = c['output']
        for key in ('failure_type', 'verified', 'conflict_flag', 'conflict_note', 'conflict_pairs',
                    'retrieval_supplement_request', 'insufficient_sq_ids'):
            if key in output: local[key] = deepcopy(output[key])
        if output.get('sub_questions'): local['sub_questions'] = deepcopy(output['sub_questions'])
        if share_evidence:
            for group in output.get('evidence_chunks', []):
                target = groups.setdefault(group['sq_id'], {**group, 'chunks': []})
                ids = {v.get('evidence_id') for v in target['chunks']}
                target['chunks'].extend(deepcopy(v) for v in group['chunks'] if v.get('evidence_id') not in ids)
    local['evidence_chunks'] = list(groups.values())
    for g in local['evidence_chunks']: g['sufficiency'] = 'unknown'
    local['expert_results'] = {c['call_id']: c['output'] for c in reads if c['kind'] in ('propose', 'dispatch', 'revise')}
    return local


def execute_phase(stage, agent_id, config, source, input_data, usage):
    local = local_state(input_data['question'], input_data['reads'], stage.share_evidence)
    local.update(input_data.get('retry_counters', {}))
    if stage.instructions and stage.kind in ('plan', 'review'):
        config['model_config']['system_prompt'] += '\n本次阶段任务：' + stage.instructions
    if stage.kind == 'lead':
        from src.runtime.protocol_agents import lead_decision
        return lead_decision(config, input_data, usage)
    if stage.kind in ('propose', 'dispatch', 'critique', 'revise', 'answer'):
        task = input_data['task']
        if stage.kind == 'dispatch':
            local['sub_questions'] = [dict(id=task['task_id'], question=task['instruction'], keywords=[], depends_on=[])]
        retrieval = retriever_fn(local, retriever=retriever_from_snapshot({'source': source}), agent_config=config)
        own = evidence_ids(retrieval['evidence_chunks'])
        local = local_state(input_data['question'], input_data['reads'] + [dict(kind='retrieve', call_id='local', output={'evidence_chunks': own})], stage.share_evidence)
        # Own retrieved evidence is always visible even when peer sharing is off.
        if not stage.share_evidence: local['evidence_chunks'] = own
        if stage.kind in ('critique', 'revise'):
            from src.runtime.protocol_agents import debate_message
            return debate_message(stage, agent_id, config, input_data, local, usage)
        if stage.kind == 'dispatch':
            local['sub_questions'] = [dict(id=task['task_id'], question=task['instruction'], keywords=[], depends_on=[])]
            config['model_config']['system_prompt'] += '\n本次分派的具体任务：' + task['instruction'] + '\n只回答该任务，完整问题由审核者汇总。'
        elif stage.instructions:
            config['model_config']['system_prompt'] += '\n本次阶段任务：' + stage.instructions
        result = answer_validator_fn(local, agent_config=config, usage_collector=usage, expert_mode=stage.kind != 'answer', require_citations=True)
        return {**result, 'evidence_chunks': local['evidence_chunks'], **({'sub_questions': local['sub_questions']} if stage.kind == 'answer' else {})}
    if stage.kind == 'plan': return planner_fn(local, agent_config=config, usage_collector=usage)
    if stage.kind == 'retrieve':
        if stage.instructions:
            local['sub_questions'] = [{**sq, 'keywords': [*sq.get('keywords', []), stage.instructions]} for sq in local['sub_questions']]
        result = retriever_fn(local, retriever=retriever_from_snapshot({'source': source}), agent_config=config)
        result['evidence_chunks'] = evidence_ids(result['evidence_chunks'])
        return result
    if stage.kind == 'review':
        config['model_config']['system_prompt'] += '\n协作背景（观点不是事实证据）：' + json.dumps(input_data['reads'], ensure_ascii=False)
        result = answer_validator_fn(local, agent_config=config, usage_collector=usage, require_citations=True)
        revisions = [c['output'].get('answer', '').strip().casefold() for c in input_data['reads'] if c['kind'] == 'revise']
        if revisions:
            result['consensus_status'] = 'agreement' if all(revisions) and len(set(revisions)) == 1 else 'disagreement' if any(revisions) else 'insufficient'
        return {**result, 'evidence_chunks': local['evidence_chunks'], 'sub_questions': local['sub_questions']}
    raise ProtocolExecutionError('协议阶段能力尚未注册。')


def build_protocol_graph(context, usage):
    snapshot = context.config_snapshot
    plan = compile_protocol(snapshot['protocol_document']['spec'], snapshot['protocol_bindings'], snapshot['instances'])
    if plan != snapshot['protocol_plan']: raise ProtocolExecutionError('协议编译结果不一致。')
    spec = parse_protocol(plan['spec'])
    v2 = spec.language_version == '2'
    stages = {s.stage_id: s for s in spec.stages}
    guard = ResourceGuard(spec.limits, usage.cancelled)
    graph = StateGraph(ProtocolState)

    def run_stage(stage, state):
        guard.check()
        writer = get_stream_writer()
        round_no = state.get('stage_visits', {}).get(stage.stage_id, 0) + 1
        reads = latest_reads(state, stage, spec.language_version)
        counters = {**{'retrieval_retry_count': 0, 'plan_retry_count': 0}, **state.get('retry_counters', {})}
        tasks = [{'agent_id': key, 'task_id': f'{stage.stage_id}_{round_no}_{i}', 'instruction': stage.instructions}
                 for i, key in enumerate(plan['bindings'][stage.slot])]
        if stage.kind == 'dispatch':
            decisions = [c for c in reads if c['kind'] == 'lead']
            if not decisions or decisions[-1]['output'].get('action') != 'dispatch':
                raise ProtocolExecutionError('分派阶段缺少本轮 Lead 决定。')
            tasks = deepcopy(decisions[-1]['output']['tasks'])
            if not tasks or any(t['agent_id'] not in plan['bindings'][stage.slot] for t in tasks):
                raise ProtocolExecutionError('分派目标不属于本阶段专家槽位。')
        outputs = {}

        def invoke(task):
            agent_id = task['agent_id']
            invocation_index = guard.start_node()
            config = deepcopy(snapshot['agents'][agent_id])
            config['_request_timeout'] = guard.remaining()
            call_id = str(uuid4())
            metadata = dict(call_id=call_id, invocation_index=invocation_index, agent_id=agent_id, stage_id=stage.stage_id, round=round_no,
                            task_id=task['task_id'], kind=stage.kind, agent_name=config['agent_name'], agent_type=config['role'])
            input_data = dict(question=state['question'], task=deepcopy(task), reads=deepcopy(reads))
            if v2: input_data['retry_counters'] = dict(counters)
            if not stage.share_evidence:
                for read in input_data['reads']:
                    for field in ('evidence_chunks', 'supporting_evidence'): read['output'].pop(field, None)
            if stage.kind == 'critique':
                participants = plan['bindings'][stage.slot]
                input_data['peer_ids'] = [k for k in participants if k != agent_id] if stage.peer_policy == 'all' else [participants[(participants.index(agent_id) + 1) % len(participants)]]
            if stage.kind == 'lead':
                input_data['candidates'] = [dict(agent_id=k, name=c['agent_name'], description=c.get('description', ''))
                    for k, c in snapshot['agents'].items() if c['role'] == 'expert']
            writer(dict(event_type='node_started', node=agent_id, cycle=round_no, payload={**metadata, 'input': input_data}))
            child = usage.for_agent(agent_id).for_agent(call_id)
            start = monotonic()
            def remaining():
                left = spec.limits.node_timeout_seconds - (monotonic() - start)
                if left <= 0: raise ProtocolExecutionError('协议任务超过时间上限。')
                return min(left, guard.remaining())
            def reserve(prompt):
                remaining()
                guard.reserve(prompt, max_tokens=config['model_config'].get('max_tokens', 0))
            child.before_invoke = reserve
            child.after_invoke = remaining
            child.invoke_adapter = lambda chat, prompt: chat.invoke(prompt, timeout=remaining())
            with usage.running_node():
                try:
                    result = execute_phase(stage, agent_id, config, snapshot.get('sources', {}).get(agent_id), input_data, child)
                    if v2 and stage.kind in ('plan', 'retrieve', 'review'): result.update(counters)
                    guard.check()
                    if monotonic() - start > spec.limits.node_timeout_seconds:
                        raise ProtocolExecutionError('协议任务超过时间上限。')
                except Exception as exc:
                    failed = {**metadata, 'input': input_data, 'output': {'error': str(exc) if isinstance(exc, ProtocolExecutionError) else '协议任务执行失败。'},
                              'status': 'failed', 'usage': asdict(child.snapshot())}
                    usage.record_output(call_id, {'protocol_calls': {call_id: failed}})
                    raise
                call = {**metadata, 'input': input_data, 'output': result, 'status': 'completed', 'usage': asdict(child.snapshot())}
                update = {'protocol_calls': {call_id: call}}
                if stage.kind == 'review' or (v2 and stage.kind == 'answer'): update.update(result)
                usage.record_output(call_id, update)
                writer(dict(event_type='node_completed', node=agent_id, cycle=round_no,
                            duration_ms=round((monotonic() - start) * 1000), payload={**metadata, 'output': update}))
                return call

        if stage.mode == 'parallel' and len(tasks) > 1:
            pool = ThreadPoolExecutor(max_workers=len(tasks))
            try:
                futures = [pool.submit(copy_context().run, invoke, t) for t in tasks]
                for future in as_completed(futures, timeout=guard.remaining()):
                    call = future.result(); outputs[call['call_id']] = call
            except FuturesTimeout as exc:
                usage.cancelled.set()
                raise ProtocolExecutionError('协议任务超过时间上限。') from exc
            except BaseException:
                usage.cancelled.set()
                raise
            finally: pool.shutdown(wait=True, cancel_futures=True)
        else:
            for task in tasks:
                call = invoke(task); outputs[call['call_id']] = call
        visits = {**state.get('stage_visits', {}), stage.stage_id: round_no}
        result = next(iter(outputs.values()))['output']
        target, reason = None, None
        for route in sorted(stage.routes, key=lambda r: r.condition == 'fallback'):
            match = (route.condition in ('always', 'fallback') or (route.condition == 'verified' and result.get('verified') is True)
                or (route.condition == 'unverified' and result.get('verified') is not True)
                or (route.condition == 'lead_dispatch' and result.get('action') == 'dispatch')
                or (route.condition == 'lead_review' and result.get('action') == 'review')
                or (v2 and route.condition == 'insufficient_retry' and not result.get('verified') and result.get('failure_type') == 'insufficient' and counters['retrieval_retry_count'] < spec.retry_limits.retrieval)
                or (v2 and route.condition == 'conflict_retry' and not result.get('verified') and result.get('failure_type') == 'conflict' and counters['plan_retry_count'] < spec.retry_limits.planning))
            if match:
                target, reason = route.target, route.condition
                if reason == 'insufficient_retry': counters['retrieval_retry_count'] += 1
                if reason == 'conflict_retry': counters['plan_retry_count'] += 1
                break
        if target is None: raise ProtocolExecutionError('没有合法的阶段出口。')
        termination = None
        if target != 'END' and visits.get(target, 0) >= stages[target].max_visits:
            target, reason = stages[target].on_limit, 'max_visits'
            if target != 'END' and visits.get(target, 0) >= stages[target].max_visits: target = 'END'
        limit_reached = state.get('limit_reached', False) or reason == 'max_visits'
        if target == 'END': termination = 'max_visits' if limit_reached else 'completed'
        state_update = {'termination_reason': termination} if termination else {}
        if v2: state_update.update(counters)
        if termination and stage.kind != 'review' and not (v2 and stage.kind == 'answer'):
            state_update.update(verified=False, verification_note='达到执行上限，新增工作尚未完成最终审核。')
        writer(dict(event_type='route_decided', node=None, cycle=round_no, payload=dict(stage_id=stage.stage_id,
            round=round_no, target=target, reason=reason, dispatched_tasks=deepcopy(tasks),
            state_update=state_update)))
        return {'protocol_calls': outputs, 'stage_visits': visits, 'next_stage': target, 'limit_reached': limit_reached,
                **({'retry_counters': counters} if v2 else {})}

    for stage in spec.stages:
        graph.add_node(stage.stage_id, lambda state, stage=stage: run_stage(stage, state))
        targets = set(stages) if not v2 else {r.target for r in stage.routes}
        if v2:
            targets |= {stages[key].on_limit for key in list(targets) if key != 'END'}
        graph.add_conditional_edges(stage.stage_id, lambda state: state['next_stage'],
                                    {**{key: key for key in targets if key != 'END'}, 'END': END})
    graph.add_edge(START, spec.entry_stage)
    compiled = graph.compile()
    compiled.stream_eager = True
    return compiled
