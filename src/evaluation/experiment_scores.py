"""Official-compatible joint metrics and descriptive, non-causal diagnostics."""
from collections import Counter

from src.evaluation.metrics import normalize_answer, score
from src.evaluation.hotpotqa import evidence_facts

METRICS = ('em', 'f1', 'sp_em', 'sp_f1', 'joint_em', 'joint_f1')


def joint_scores(prediction, facts, gold):
    scores = score(prediction, facts, gold)
    predicted, expected = normalize_answer(prediction), normalize_answer(gold['answer'])
    common = sum((Counter(predicted.split()) & Counter(expected.split())).values())
    if predicted != expected and ({predicted, expected} & {'yes', 'no', 'noanswer'}):
        common = 0
    ap = common / len(predicted.split()) if predicted.split() else 0.0
    ar = common / len(expected.split()) if expected.split() else 0.0
    ps, gs = set(map(tuple, facts)), set(map(tuple, gold['supporting_facts']))
    sp = len(ps & gs) / len(ps) if ps else 0.0
    sr = len(ps & gs) / len(gs) if gs else 0.0
    precision, recall = ap * sp, ar * sr
    return {**scores, 'joint_em': scores['em'] * scores['sp_em'],
            'joint_f1': 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def score_detail(detail, record):
    run = detail.run
    state = run.final_state or {}
    answer = state.get('answer') or ''
    evidence = state.get('supporting_evidence', [])
    facts, _ = evidence_facts(evidence, record['context'])
    scores = joint_scores(answer, facts, record)
    if run.status != 'completed':
        scores = dict.fromkeys(METRICS, 0.0)
    labels = []
    if run.status != 'completed':
        labels.append('runtime_' + run.status)
    if not scores['em']:
        labels.append('answer_mismatch')
    if scores['em'] and not scores['sp_em']:
        labels.append('correct_answer_support_mismatch')
    verified = bool(state.get('verified')) and run.status == 'completed'
    if verified and not scores['em']: labels.append('verified_but_answer_mismatch')
    if not verified and scores['em']: labels.append('unverified_but_answer_match')
    calls = sorted(state.get('protocol_calls', {}).values(), key=lambda c: c.get('invocation_index', 0))
    all_facts, initial, seen_agents = set(), set(), set()
    mapped, raw_count, retrieval_calls, no_new = 0, 0, 0, 0
    for call in calls:
        output = call.get('output', {})
        if output.get('failure_type') == 'output_error': labels.append('structured_output_error')
        groups = output.get('retrieved_evidence_chunks')
        if groups is None:
            continue  # Older traces: unknown, not inferred from final citations or peer evidence.
        retrieval_calls += 1
        chunks = [c for group in groups for c in group.get('chunks', [])]
        located = []
        for chunk in chunks:
            value, _ = evidence_facts([chunk], record['context'])
            mapped += bool(value)
            located.extend(value)
        raw_count += len(chunks)
        coords = set(map(tuple, located))
        if call['agent_id'] not in seen_agents:
            initial |= coords
            seen_agents.add(call['agent_id'])
        elif not coords - all_facts:
            no_new += 1
        all_facts |= coords
    gold_facts = set(map(tuple, record['supporting_facts']))
    reliable = retrieval_calls > 0 and mapped == raw_count
    coverage = len(all_facts & gold_facts) / len(gold_facts) if reliable else None
    initial_coverage = len(initial & gold_facts) / len(gold_facts) if reliable else None
    if coverage is not None and coverage < 1: labels.append('gold_support_not_fully_retrieved')
    if coverage == 1 and not scores['em']: labels.append('support_covered_answer_mismatch')
    if no_new: labels.append('retrieval_without_new_evidence')
    error = run.error.model_dump() if run.error else None
    error_text = (error or {}).get('message', '')
    if any(word in error_text.lower() for word in ('超时', '时间上限', 'timeout')): labels.append('timeout')
    if any(word in error_text for word in ('预算', '上限', '耗尽')) or state.get('termination_reason') == 'max_visits':
        labels.append('resource_limit')
    if state.get('failure_type') == 'output_error': labels.append('structured_output_error')
    if len(facts) < len(evidence): labels.append('invalid_or_duplicate_final_reference')
    return dict(sample_id=record['_id'], question=record['question'], type=record['type'],
                run_id=run.context.run_id, method_id=run.context.method_id, status=run.status,
                answer=answer, verified=verified, supporting_evidence=evidence,
                predicted_supporting_facts=facts, scores=scores, metrics=run.metrics.model_dump(),
                error=error, labels=sorted(set(labels)), gold={'answer': record['answer'], 'supporting_facts': record['supporting_facts']},
                config_sha256=run.context.config_snapshot['config_sha256'],
                retrieval={'initial_support_coverage': initial_coverage, 'cumulative_support_coverage': coverage,
                           'calls': retrieval_calls, 'without_new_evidence': no_new,
                           'mapped_chunks': mapped, 'raw_chunks': raw_count})
