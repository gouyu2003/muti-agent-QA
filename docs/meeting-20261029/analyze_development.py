"""Read the completed development archive; never access evaluation answers or call models."""
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
DEV = ROOT / 'data/experiments/baseline-dev-001'
REPORT = DEV / 'reports/20261027T074950773932Z'
read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
summary = read(REPORT / 'summary.json')
rows = list(csv.DictReader((REPORT / 'per-question.csv').open(encoding='utf-8-sig', newline='')))
assert len(rows) == 600
assert len({(r['sample_id'], r['method_id'], r['repeat']) for r in rows}) == 600
assert summary['complete'] and summary['retry_count'] == 0
score_module = runpy.run_path(str(ROOT / 'src/evaluation/metrics.py'))
normalize = score_module['normalize_answer']
answer_scores = score_module['answer_scores']
grouped = defaultdict(dict)
for row in rows:
    row['scores'] = json.loads(row['scores'])
    grouped[row['sample_id']][row['method_id']] = row

results, details, hypotheses = {}, {}, {}
for method, aggregate in summary['methods'].items():
    selected = [r for r in rows if r['method_id'] == method]
    for metric, expected in aggregate['scores'].items():
        assert math.isclose(sum(r['scores'][metric] for r in selected) / 100, expected, abs_tol=1e-10)
    terms, empty_types, verification, stages = Counter(), Counter(), Counter(), Counter()
    initial_correct_final_wrong, matched_proposals, proposal_cases = [], [], []
    cases = {}
    for row in selected:
        export = read(DEV / 'runs' / (row['run_id'] + '.json'))
        run = export['run']; state = run.get('final_state') or {}
        terms[str(state.get('termination_reason'))] += 1
        if not row['answer'].strip():
            empty_types[str(state.get('failure_type'))] += 1
        if state.get('verified'):
            verification['verified'] += 1
            verification['verified_em_correct' if row['scores']['em'] else 'verified_em_wrong'] += 1
        elif row['scores']['em']:
            verification['unverified_em_correct'] += 1
        events = export['events']
        if state.get('termination_reason') == 'max_visits':
            completed = [e for e in events if e['event_type'] == 'node_completed']
            stages[completed[-1]['node'] if completed else 'none'] += 1
        calls = state.get('protocol_calls') or {}
        proposed = [c for c in calls.values() if c['kind'] == 'propose' and c['status'] == 'completed']
        proposals = [dict(agent=c['agent_id'], answer=c['output'].get('answer', '')) for c in proposed]
        gold = json.loads(row['gold'])['answer']
        if proposals:
            proposal_cases.append(row['sample_id'])
            if len(proposals) >= 2 and len({normalize(p['answer']) for p in proposals}) == 1:
                matched_proposals.append(row['sample_id'])
            if any(answer_scores(p['answer'], gold)['em'] for p in proposals) and not row['scores']['em']:
                initial_correct_final_wrong.append(row['sample_id'])
        diagnostic = dict(question=run['context']['question'], gold=gold, answer=row['answer'], status=row['status'],
            scores=row['scores'], run_id=row['run_id'], termination_reason=state.get('termination_reason'),
            failure_type=state.get('failure_type'), verified=state.get('verified'),
            verification_note=state.get('verification_note'), initial_proposals=proposals)
        cases[row['sample_id']] = diagnostic
    definition = read(DEV / 'methods' / (method + '.json'))
    snapshot = definition['snapshot']
    results[method] = dict(status_counts=aggregate['status_counts'], scores=aggregate['scores'],
        mean_calls=aggregate['metrics']['llm_call_count']['mean_reported'],
        mean_seconds=aggregate['metrics']['duration_ms']['mean_reported'] / 1000,
        median_seconds=aggregate['metrics']['duration_ms']['median_reported'] / 1000,
        p95_seconds=aggregate['metrics']['duration_ms']['p95_reported'] / 1000,
        metrics=aggregate['metrics'], labels=aggregate['labels'],
        initial_support_coverage=aggregate['initial_support_coverage']['mean_reported'],
        cumulative_support_coverage=aggregate['cumulative_support_coverage']['mean_reported'],
        by_type={t: v['scores'] for t, v in aggregate['by_type'].items()},
        termination_counts=dict(terms), last_node_at_limit=dict(stages),
        empty_answer_failure_types=dict(empty_types), verification=dict(verification),
        stages=[dict(kind=s['kind'], slot=s['slot'], reads=s['reads'], routes=s['routes'])
                for s in snapshot['protocol_document']['spec']['stages']],
        agent_roles=dict(Counter(a['agent_type'] for a in snapshot['instances'])))
    hypotheses[method] = dict(proposal_question_count=len(proposal_cases),
        matching_initial_answers_count=len(matched_proposals),
        initially_correct_final_wrong_count=len(initial_correct_final_wrong),
        initially_correct_final_wrong_sample_ids=initial_correct_final_wrong)
    details[method] = cases

case_ids = {}
improvements = [sid for sid, methods in grouped.items() if methods['pipeline_fixed']['scores']['em'] and not methods['single_rag']['scores']['em']]
case_ids['pipeline_improvement'] = sorted(improvements, key=lambda sid: grouped[sid]['pipeline_fixed']['scores']['joint_f1'], reverse=True)[:2]
losses = hypotheses['debate']['initially_correct_final_wrong_sample_ids']
case_ids['debate_loses_correct_proposal'] = sorted(losses, key=lambda sid: (details['debate'][sid]['answer'] != '', sid))[:2]
case_ids['debate_correct_pipeline_wrong'] = [sid for sid, methods in grouped.items()
    if methods['debate']['scores']['em'] and not methods['pipeline_fixed']['scores']['em']][:2]
examples = {category: [dict(sample_id=sid, methods={m:details[m][sid] for m in results}) for sid in ids]
            for category, ids in case_ids.items()}

paired = [p for p in summary['pairs'] if p['left'] == 'single_rag'
          or (p['left'] == 'pipeline_fixed' and p['right'] == 'pipeline_adaptive')]
archive = read(ROOT / 'data/experiments/diagnostics/evaluation-preflight-20261028-8b5c0bb.json')
for relative, expected in archive['archive_hashes_unchanged'].items():
    assert hashlib.sha256((ROOT/'data/experiments'/relative).read_bytes()).hexdigest().upper() == expected
output = dict(experiment='baseline-dev-001', question_count=100, task_count=600,
    status_counts=dict(Counter(r['status'] for r in rows)), sessions=summary['sessions'],
    methods=results, paired=paired, exploratory_trajectory_counts=hypotheses, cases=examples,
    caveats=['Development observations; no independent test conclusion.',
        '100 paired question units, not 600 independent samples.',
        'Stored bootstrap intervals are unadjusted and exploratory.',
        'Questions selected for illustration are not representative samples.'],
    source_summary_sha256=hashlib.sha256((REPORT/'summary.json').read_bytes()).hexdigest(),
    protected_archive_files_unchanged=len(archive['archive_hashes_unchanged']))
(OUT/'development-analysis.json').write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(json.dumps(dict(status_counts=output['status_counts'], hypotheses=hypotheses,
    paired=[dict(left=p['left'],right=p['right'],f1=p['metrics']['f1'],joint_f1=p['metrics']['joint_f1']) for p in paired],
    case_ids=case_ids,methods={m:dict(stages=[s['kind'] for s in r['stages']],verification=r['verification'],
        mean_seconds=r['mean_seconds'],coverage=r['cumulative_support_coverage']) for m,r in results.items()}), indent=2))
