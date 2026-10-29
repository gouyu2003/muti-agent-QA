"""Read frozen first-attempt evaluation data; never call models or rewrite archives."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
EVAL = ROOT / 'data/experiments/baseline-evaluation-001'
REPORT = EVAL / 'reports/20261028T232306054658Z'
DEV = ROOT / 'data/experiments/baseline-dev-001'
read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


protected = [EVAL / name for name in ('config.resolved.json', 'manifest.json', 'provenance.json',
    'source.zip', 'results.jsonl', 'runs.sqlite3', 'tasks.sqlite3', 'logs/progress.jsonl')]
protected += sorted((EVAL / 'methods').glob('*.json'))
protected += [p for p in REPORT.iterdir() if p.is_file()]
protected += [DEV / name for name in ('manifest.json', 'provenance.json', 'results.jsonl')]
before = {str(path.relative_to(ROOT)): sha(path) for path in protected}
summary = read(REPORT / 'summary.json')
development = read(ROOT / 'docs/meeting-20261029/development-analysis.json')
config = read(EVAL / 'config.resolved.json')
rows = [json.loads(line) for line in (EVAL / 'results.jsonl').open(encoding='utf-8') if line.strip()]
methods = list(summary['methods'])
assert summary['complete'] and summary['retry_count'] == 0
assert len(rows) == 6000 and all(r['attempt'] == 1 and r['repeat'] == 0 for r in rows)
assert len({(r['sample_id'], r['method_id']) for r in rows}) == 6000
by_id, by_method = defaultdict(dict), defaultdict(list)
for row in rows:
    by_id[row['sample_id']][row['method_id']] = row
    by_method[row['method_id']].append(row)
assert len(by_id) == 1000 and all(set(group) == set(methods) for group in by_id.values())
for method, group in by_method.items():
    assert len(group) == 1000
    for metric, expected in summary['methods'][method]['scores'].items():
        assert math.isclose(sum(r['scores'][metric] for r in group) / 1000, expected, abs_tol=1e-12)
    assert dict(Counter(r['status'] for r in group)) == summary['methods'][method]['status_counts']

uri = (EVAL / 'runs.sqlite3').resolve().as_uri() + '?mode=ro&immutable=1'
with sqlite3.connect(uri, uri=True) as db:
    db.row_factory = sqlite3.Row
    state_rows = db.execute('''SELECT run_id, method_id, status, question,
        json_extract(final_state_json, '$.termination_reason') termination_reason,
        json_extract(final_state_json, '$.failure_type') failure_type,
        json_extract(final_state_json, '$.answer') answer,
        json_extract(final_state_json, '$.verified') verified,
        json_extract(final_state_json, '$.verification_note') verification_note
        FROM runs''').fetchall()
    states = {row['run_id']: dict(row) for row in state_rows}
    for state in states.values():
        if state['termination_reason'] == 'max_visits':
            last = db.execute("SELECT node FROM trace_events WHERE run_id=? AND event_type='node_completed' ORDER BY sequence DESC LIMIT 1",
                              (state['run_id'],)).fetchone()
            state['last_completed_node'] = last[0] if last else None
assert len(states) == 6000
assert all(states[row['run_id']]['method_id'] == row['method_id'] and
           states[row['run_id']]['status'] == row['status'] for row in rows)

failures, failure_groups, failed_samples = [], defaultdict(Counter), set()
request_outcomes = Counter()
request_recovered_tasks = 0
call_usage = dict(prompt_tokens=0, completion_tokens=0, total_tokens=0, records=0, known_records=0)
request_codes = Counter()
for row in rows:
    usage = row['metrics'].get('agent_usage', {})
    for agent, value in usage.items():
        for call in value.get('calls', []):
            call_usage['records'] += 1
            if call.get('total_tokens') is not None:
                call_usage['known_records'] += 1
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                if call.get(key) is not None:
                    call_usage[key] += call[key]
            request_outcomes[call.get('outcome', 'not_recorded')] += 1
            if call.get('outcome') == 'request_error':
                error = call.get('request_error') or {}
                request_codes[(error.get('category'), error.get('provider_code'), error.get('http_status'))] += 1
    if row['status'] == 'completed' and 'request_retry_recovered' in row.get('labels', []):
        request_recovered_tasks += 1
    if row['status'] != 'failed':
        continue
    error = row.get('error') or {}
    message = error.get('message', '')
    if error.get('error_type') == 'provider_content_filter':
        category = 'content_filter'
    elif message == '协议任务超过时间上限。':
        category = 'protocol_timeout'
    elif message == '专家未返回合法的质疑或修订消息。':
        category = 'debate_output_validation'
    else:
        category = 'other'
    failure_groups[row['method_id']][category] += 1
    failed_samples.add(row['sample_id'])
    detail = read(EVAL / 'runs' / (row['run_id'] + '.json'))
    failed_calls = [call for call in (detail['run'].get('final_state') or {}).get('protocol_calls', {}).values()
                    if call['status'] == 'failed']
    terminal = max(failed_calls, key=lambda call: call['invocation_index']) if failed_calls else {}
    agent_id = terminal.get('agent_id')
    recorded = detail['run']['metrics'].get('agent_usage', {}).get(agent_id, {}).get('calls', [])
    tail = recorded[-3:]
    diagnostics = [call['request_error'] for value in detail['run']['metrics'].get('agent_usage', {}).values()
                   for call in value.get('calls', []) if call.get('request_error')]
    started = [event for event in detail['events'] if event['event_type'] == 'node_started'
               and event['payload'].get('call_id') == terminal.get('call_id')]
    node_seconds = None
    if started and detail['run'].get('finished_at'):
        node_seconds = (datetime.fromisoformat(detail['run']['finished_at']) -
                        datetime.fromisoformat(started[-1]['timestamp'])).total_seconds()
    failures.append(dict(method=row['method_id'], sample_id=row['sample_id'], run_id=row['run_id'],
        category=category, error_type=error.get('error_type'), message=message,
        stage=terminal.get('stage_id'), kind=terminal.get('kind'), agent_id=agent_id,
        node_elapsed_seconds=node_seconds,
        request_diagnostics=diagnostics,
        last_call_outcomes=[call.get('outcome') for call in tail],
        last_validation_errors=[call.get('validation_errors') for call in tail]))
assert len(failures) == 92
schema_failures = [r for r in failures if r['category'] == 'debate_output_validation']
assert len(schema_failures) == 25 and all(r['last_call_outcomes'] == ['invalid_schema'] * 3 for r in schema_failures)

method_details = {}
for method in methods:
    group, aggregate = by_method[method], summary['methods'][method]
    state = [states[row['run_id']] for row in group]
    verification = Counter()
    for row in group:
        if row['verified']:
            verification['verified'] += 1
            verification['verified_em_match' if row['scores']['em'] else 'verified_em_mismatch'] += 1
        elif row['scores']['em']:
            verification['unverified_em_match'] += 1
    complete_empty = [r for r in group if r['status'] == 'completed' and not r['answer'].strip()]
    all_empty = [r for r in group if not r['answer'].strip()]
    limited = [s for s in state if s['termination_reason'] == 'max_visits']
    completed = [r for r in group if r['status'] == 'completed']
    method_details[method] = dict(
        status_counts=aggregate['status_counts'], scores=aggregate['scores'], metrics=aggregate['metrics'],
        coverage={key: aggregate[key] for key in ('initial_support_coverage', 'cumulative_support_coverage')},
        by_type={key: dict(n=value['planned'], scores=value['scores']) for key, value in aggregate['by_type'].items()},
        labels=aggregate['labels'], failures=dict(failure_groups[method]),
        empty_answers=len(all_empty), completed_empty_answers=len(complete_empty),
        completed_empty_failure_types=dict(Counter(states[r['run_id']]['failure_type'] for r in complete_empty)),
        termination_counts=dict(Counter(str(s['termination_reason']) for s in state)),
        max_visits=len(limited), last_node_at_limit=dict(Counter(str(s.get('last_completed_node')) for s in limited)),
        verification=dict(verification),
        completed_only_mean_f1=sum(r['scores']['f1'] for r in completed) / len(completed),
        failure_perfect_upper_f1=aggregate['scores']['f1'] + aggregate['status_counts'].get('failed', 0) / 1000,
        dev_f1=development['methods'][method]['scores']['f1'],
        dev_mean_calls=development['methods'][method]['mean_calls'],
        dev_mean_seconds=development['methods'][method]['mean_seconds'])

common_ids = [sid for sid, group in by_id.items() if all(row['status'] == 'completed' for row in group.values())]
common_completed = {method: sum(by_id[sid][method]['scores']['f1'] for sid in common_ids) / len(common_ids) for method in methods}


def case(sid):
    return dict(sample_id=sid, question=by_id[sid][methods[0]]['question'],
        gold=by_id[sid][methods[0]]['gold']['answer'],
        methods={method: dict(run_id=row['run_id'], answer=row['answer'], status=row['status'],
                             scores=row['scores'], coverage=row['retrieval']['cumulative_support_coverage'],
                             verification_note=states[row['run_id']]['verification_note'])
                 for method, row in by_id[sid].items()})


example_ids = {}
eligible = [sid for sid, group in by_id.items() if group['pipeline_fixed']['scores']['em'] and
    not group['single_rag']['scores']['em'] and group['pipeline_fixed']['retrieval']['cumulative_support_coverage'] == 1
    and group['single_rag']['retrieval']['cumulative_support_coverage'] is not None
    and group['single_rag']['retrieval']['cumulative_support_coverage'] < 1]
example_ids['pipeline_improvement'] = sorted(eligible)[:3]
example_ids['debate_abstention'] = sorted(sid for sid, group in by_id.items()
    if group['pipeline_fixed']['scores']['em'] and group['debate']['status'] == 'completed' and not group['debate']['answer'].strip())[:3]
example_ids['debate_correct_pipeline_wrong'] = sorted(sid for sid, group in by_id.items()
    if group['debate']['scores']['em'] and not group['pipeline_fixed']['scores']['em'])[:2]

dataset = config['dataset']
manifest = read(EVAL / 'manifest.json')
dev_manifest = read(DEV / 'manifest.json')
assert not ({r['sample_id'] for r in rows} & {r['sample_id'] for r in
    [json.loads(line) for line in (DEV / 'results.jsonl').open(encoding='utf-8') if line.strip()]})
spec = importlib.util.spec_from_file_location('evaluation_source_version', ROOT / 'src/runtime/version.py')
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)
assert version.source_fingerprint() == summary['provenance']['identity']['source_sha256']

output = dict(experiment='baseline-evaluation-001', report=str(REPORT.relative_to(ROOT)),
    questions=1000, tasks=6000, repeats=1, paired_unit='question',
    status_counts=dict(Counter(row['status'] for row in rows)),
    task_level_retries=summary['retry_count'], sessions=summary['sessions'],
    methods=method_details, paired=summary['pairs'], bootstrap=summary['bootstrap'],
    all_attempt_metrics=summary['all_attempt_costs'], per_call_usage=call_usage,
    recorded_call_outcomes=dict(request_outcomes),
    request_error_codes=[dict(category=key[0], provider_code=key[1], http_status=key[2], calls=value)
                         for key, value in request_codes.items()],
    recovered_request_error_tasks=request_recovered_tasks,
    failure_categories=dict(Counter(row['category'] for row in failures)),
    failed_questions=len(failed_samples),
    failure_stages={category: dict(Counter(row['stage'] for row in failures if row['category'] == category))
                    for category in sorted({row['category'] for row in failures})},
    common_completed_sensitivity=dict(questions=len(common_ids), f1=common_completed,
        note='Post-outcome selection; sensitivity only, not the primary evaluation set.'),
    examples={key: [case(sid) for sid in ids] for key, ids in example_ids.items()},
    source_sha256=summary['provenance']['identity']['source_sha256'],
    protected_file_hashes=before,
    caveats=['Internal split of public HotpotQA dev-distractor, not official hidden test.',
             'One real run per question and method, not replicated seeds.',
             'Bootstrap intervals are unadjusted and resample questions, not provider variability.',
             'Shared ceilings do not match actual calls or evidence volume.',
             'Unknown token usage is not zero; no monetary bill is inferred.'])
after = {str(path.relative_to(ROOT)): sha(path) for path in protected}
assert before == after
(OUT / 'analysis.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
with (OUT / 'failures.csv').open('w', encoding='utf-8-sig', newline='') as handle:
    fields = list(failures[0])
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    for row in failures:
        writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value for key, value in row.items()})
(OUT / 'archive-integrity.json').write_text(json.dumps(dict(checked=len(before), unchanged=True,
    files=before, source_matches_frozen=True, new_model_calls=0), indent=2) + '\n', encoding='utf-8')
print(json.dumps(dict(status=output['status_counts'], failures=output['failure_categories'],
    stages=output['failure_stages'], call_usage=call_usage, recovered=output['recovered_request_error_tasks'],
    failed_questions=output['failed_questions'], common_completed=output['common_completed_sensitivity'],
    methods={method: {key: data[key] for key in ('failures', 'empty_answers', 'completed_empty_answers',
        'max_visits', 'last_node_at_limit', 'verification')} for method, data in method_details.items()},
    example_ids=example_ids, protected_unchanged=len(before)), ensure_ascii=False, indent=2))
