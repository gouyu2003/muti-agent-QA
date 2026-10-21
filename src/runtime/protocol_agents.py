"""Structured public collaboration messages, with evidence checked separately."""
import json
from typing import Literal
from pydantic import Field, model_validator
from src.runtime.specs import StrictSpec
from src.agents import answer_validator
from src.agents.structured_output import SchemaViolation, invoke_json


class Assignment(StrictSpec):
    task_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    agent_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    instruction: str = Field(min_length=1, max_length=2000)


class LeadDecision(StrictSpec):
    action: Literal['dispatch', 'review']
    tasks: list[Assignment] = Field(default_factory=list, max_length=3)
    summary: str = Field(default='', max_length=6000)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def valid_tasks(self):
        if (self.action == 'dispatch') != bool(self.tasks):
            raise SchemaViolation('action_tasks_mismatch', 'dispatch 必须包含任务，review 不得包含任务。', 'tasks')
        if len({t.task_id for t in self.tasks}) != len(self.tasks):
            raise SchemaViolation('duplicate_task_id', '本轮任务 ID 不能重复。', 'tasks')
        if any(not t.instruction.strip() for t in self.tasks):
            raise SchemaViolation('blank_instruction', '任务内容不能为空。', 'tasks')
        if self.action == 'review' and not self.summary.strip():
            raise SchemaViolation('missing_summary', '提交审核时需给出汇总答案。', 'summary')
        return self


def lead_decision(config, input_data, usage):
    candidates = {c['agent_id'] for c in input_data['candidates']}
    completed = any(c['kind'] == 'dispatch' for c in input_data['reads'])
    schema = LeadDecision.model_json_schema()
    schema['$defs']['Assignment']['properties']['agent_id']['enum'] = sorted(candidates)
    examples = {}
    if candidates:
        examples['dispatch'] = {'action': 'dispatch', 'tasks': [{
            'task_id': 'lookup_1', 'agent_id': input_data['candidates'][0]['agent_id'],
            'instruction': 'Find the specific fact assigned to this expert in its bound materials.'}],
            'summary': '', 'reason': 'This expert covers the required fact.'}
    if completed:
        examples['review'] = {'action': 'review', 'tasks': [],
            'summary': 'Summarize only facts actually supported by completed_results; state any missing facts.',
            'reason': 'The available expert results are ready for review.'}
    prompt = config['model_config']['system_prompt'] + '\n' + json.dumps({
        'stage': 'lead', 'question': input_data['question'], 'instructions': input_data['task']['instruction'],
        'candidates': input_data['candidates'], 'completed_results': input_data['reads'],
        'output_contract': schema, 'format_examples': examples,
        'allowed_actions': ['dispatch', 'review'] if completed else ['dispatch'],
        'rules': 'Return exactly one JSON object matching output_contract, with no Markdown or extra fields. '
                 'action must be exactly dispatch or review, never the phrase "dispatch or review". '
                 'On the first turn you MUST dispatch; review is allowed only after completed expert results exist. '
                 'Dispatch 1 to 3 concrete tasks only to relevant registered candidates. Copy agent_id exactly, not the name. '
                 'Use unique task_id values matching ^[A-Za-z][A-Za-z0-9_-]{0,63}$. Instructions must not be blank. '
                 'The examples demonstrate format only: select experts and write tasks for the actual question. '
                 'Never claim evidence not supplied. After inspecting results, dispatch missing work or review with '
                 'tasks=[] and a nonblank concise summary. Always provide a nonempty reason.'}, ensure_ascii=False)
    def validate(data):
        decision = LeadDecision.model_validate(data)
        for index, task in enumerate(decision.tasks):
            if task.agent_id not in candidates:
                raise SchemaViolation('unregistered_agent', '目标专家未注册，请原样复制 candidates 中的 agent_id。', f'tasks.{index}.agent_id')
        if decision.action == 'review' and not completed:
            raise SchemaViolation('review_before_dispatch', '至少完成一轮专家任务后才能汇总；本轮必须 dispatch。', 'action')
    result, diagnostics = invoke_json(answer_validator.default_llm(config), prompt,
        config.get('failure_handling', {}).get('max_retries', 2), usage, validate=validate,
        max_tokens=config['model_config']['max_tokens'])
    if result is None:
        from src.runtime.protocol_runtime import ProtocolExecutionError
        issues = diagnostics['calls'][-1].get('validation_errors', [])
        detail = '；'.join(f"{e['path']}: {e['message']}" for e in issues[:3])
        raise ProtocolExecutionError('Lead 未返回合法的分派决定。' + (f'最后一次校验：{detail}' if detail else ''))
    return {**LeadDecision.model_validate(result).model_dump(), 'model_diagnostics': diagnostics}


class Challenge(StrictSpec):
    target_call_id: str = Field(min_length=1, max_length=100)
    stance: Literal['agree', 'challenge', 'missing']
    issue: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class Critique(StrictSpec):
    critiques: list[Challenge] = Field(min_length=1, max_length=3)
    summary: str = Field(min_length=1, max_length=3000)


class Response(StrictSpec):
    critique_call_id: str = Field(min_length=1, max_length=100)
    accepted: bool
    response: str = Field(min_length=1, max_length=2000)


class Reference(StrictSpec):
    sq_id: str = Field(min_length=1, max_length=100)
    evidence_id: str = Field(min_length=1, max_length=100)


class Revision(StrictSpec):
    answer: str = Field(max_length=8000)
    verified: bool
    failure_type: Literal['none', 'insufficient', 'conflict']
    supporting_evidence: list[Reference] = Field(default_factory=list, max_length=50)
    verification_note: str = Field(min_length=1, max_length=3000)
    responses: list[Response] = Field(default_factory=list, max_length=6)
    change_note: str = Field(min_length=1, max_length=3000)


def current_opinions(reads):
    result = {}
    for call in reads:
        if call['kind'] not in ('propose', 'revise'): continue
        old = result.get(call['agent_id'])
        if old is None or (call['kind'] == 'revise' and (old['kind'] != 'revise' or call['round'] >= old['round'])):
            result[call['agent_id']] = call
    return result


def debate_message(stage, agent_id, config, input_data, local, usage):
    opinions = current_opinions(input_data['reads'])
    peer_ids = input_data.get('peer_ids', [])
    targets = {opinions[key]['call_id'] for key in peer_ids if key in opinions}
    mine = {c['call_id'] for c in input_data['reads'] if c['agent_id'] == agent_id and c['kind'] in ('propose', 'revise')}
    incoming = {c['call_id'] for c in input_data['reads'] if c['kind'] == 'critique'
                and any(ch['target_call_id'] in mine for ch in c['output'].get('critiques', []))}
    known = {(g['sq_id'], c['evidence_id']) for g in local['evidence_chunks'] for c in g['chunks']}
    evidence_ids = {e for _, e in known}
    contract = Critique if stage.kind == 'critique' else Revision
    prompt = config['model_config']['system_prompt'] + '\n' + json.dumps({
        'stage': stage.kind, 'agent_id': agent_id, 'question': input_data['question'],
        'instructions': stage.instructions, 'previous_public_messages': input_data['reads'],
        'target_call_ids': sorted(targets), 'incoming_critique_call_ids': sorted(incoming),
        'evidence': local['evidence_chunks'], 'output_schema': contract.model_json_schema(),
        'rules': 'Return only the specified JSON. Opinions are not facts. Agree honestly when evidence agrees; do not invent disagreement. '
                 'Critique every target_call_id once using supplied evidence_ids, or flag missing evidence. '
                 'For revision respond to each incoming_critique_call_id once, explain accepted/rejected changes, '
                 'preserve unsupported uncertainties, and cite only exact supplied sq_id/evidence_id pairs.'}, ensure_ascii=False)
    def validate(data):
        msg = contract.model_validate(data)
        if stage.kind == 'critique':
            ids = [c.target_call_id for c in msg.critiques]
            if not targets or set(ids) != targets or len(set(ids)) != len(ids): raise ValueError('质疑目标必须对应当前同伴观点。')
            if any(not set(c.evidence_ids) <= evidence_ids for c in msg.critiques): raise ValueError('质疑引用了未知证据。')
        else:
            ids = [r.critique_call_id for r in msg.responses]
            if set(ids) != incoming or len(ids) != len(set(ids)): raise ValueError('修订必须回应每条针对本人的质疑消息。')
            if msg.answer.strip() and not msg.supporting_evidence: raise ValueError('修订答案必须附可追溯引用。')
            if any((r.sq_id, r.evidence_id) not in known for r in msg.supporting_evidence): raise ValueError('修订引用无法定位。')
    result, diagnostics = invoke_json(answer_validator.default_llm(config), prompt,
        config.get('failure_handling', {}).get('max_retries', 2), usage, validate=validate,
        max_tokens=config['model_config']['max_tokens'])
    if result is None:
        from src.runtime.protocol_runtime import ProtocolExecutionError
        raise ProtocolExecutionError('专家未返回合法的质疑或修订消息。')
    result = contract.model_validate(result).model_dump()
    if stage.kind == 'revise':
        parsed = answer_validator.parse_verification(json.dumps(result), local, require_citations=True)
        result.update(parsed)
    return {**result, 'model_diagnostics': diagnostics, 'evidence_chunks': local['evidence_chunks']}
