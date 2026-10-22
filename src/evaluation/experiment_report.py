"""Local-only, versioned reports. First attempts and retries are never pooled for quality."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from itertools import combinations
import json
from pathlib import Path
import numpy as np
from statistics import mean, stdev
from datetime import datetime, timezone

from src.evaluation.experiment_config import digest, atomic_json
from src.evaluation.experiment_scores import METRICS
from src.runtime.version import source_fingerprint


def percentile(values, fraction):
    if not values: return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (index - lower)


def summary(rows, planned):
    statuses = Counter(r['status'] for r in rows)
    finished = sum(statuses[s] for s in ('completed', 'failed'))
    complete = len(rows) == planned and finished == planned
    scored = [r for r in rows if r.get('scores')]
    result = dict(planned=planned, attempted=len(rows), unstarted=planned - len(rows),
                  status_counts=dict(statuses), complete=complete,
                  completion_rate=statuses['completed'] / planned if planned else None,
                  failure_rate=statuses['failed'] / planned if planned else None,
                  verified_rate_completed=(sum(r.get('verified', False) for r in rows) / statuses['completed']
                                           if statuses['completed'] else None),
                  scores={name: sum(r['scores'][name] for r in scored) / planned if complete else None for name in METRICS},
                  observed_scores={name: mean(r['scores'][name] for r in scored) if scored else None for name in METRICS},
                  labels=dict(Counter(label for r in rows for label in r.get('labels', []))))
    result['metrics'] = {}
    for metric in ('llm_call_count', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'duration_ms'):
        values = [r.get('metrics', {}).get(metric) for r in rows]
        known = [v for v in values if v is not None]
        result['metrics'][metric] = dict(reported_total=sum(known) if known else None,
            mean_reported=mean(known) if known else None, reported=len(known), attempted=len(rows),
            median_reported=percentile(known, .5), p95_reported=percentile(known, .95))
    for key in ('initial_support_coverage', 'cumulative_support_coverage'):
        values = [r.get('retrieval', {}).get(key) for r in rows]
        known = [v for v in values if v is not None]
        result[key] = dict(mean_reported=mean(known) if known else None, reported=len(known))
    return result


def comparisons(rows, config, selected_ids):
    indexed = defaultdict(list)
    for row in rows:
        indexed[(row['method_id'], row['sample_id'])].append(row)
    grouped = {}
    for method in config.methods:
        grouped[method] = {}
        for sample_id in selected_ids:
            sample_rows = indexed[(method, sample_id)]
            if len(sample_rows) == config.repeats and all(r['status'] in ('completed', 'failed') for r in sample_rows):
                grouped[method][sample_id] = {key: mean(r['scores'][key] for r in sample_rows) for key in METRICS}
    pairs = []
    for left, right in combinations(config.methods, 2):
        ids = sorted(grouped[left].keys() & grouped[right].keys())
        result = dict(left=left, right=right, paired_samples=len(ids), planned_samples=len(selected_ids),
                      complete=len(ids) == len(selected_ids), direction='right minus left', metrics={})
        for metric in METRICS:
            deltas = [grouped[right][i][metric] - grouped[left][i][metric] for i in ids]
            if not result['complete'] or not deltas:
                result['metrics'][metric] = None
                continue
            rng = np.random.default_rng(int(digest([config.seed, left, right, metric])[:16], 16))
            # Bounded batches avoid an O(samples * replicates) memory peak on full dev.
            values = np.asarray(deltas)
            distribution = []
            for start in range(0, config.bootstrap_samples, 100):
                count = min(100, config.bootstrap_samples - start)
                distribution.extend(values[rng.integers(0, len(values), size=(count, len(values)))].mean(axis=1).tolist())
            result['metrics'][metric] = dict(delta=mean(deltas), right_wins=sum(d > 0 for d in deltas),
                ties=sum(d == 0 for d in deltas), left_wins=sum(d < 0 for d in deltas),
                ci95_sample_bootstrap=[percentile(distribution, .025), percentile(distribution, .975)])
        pairs.append(result)
    return pairs


def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row[key], ensure_ascii=False) if isinstance(row.get(key), (dict, list))
                             else row.get(key, '') for key in fields})


def generate_report(directory, config, records, ledger, attempts, provenance):
    directory = Path(directory)
    rows = [{**a['result'], 'attempt': a['attempt'], 'repeat': a['repeat'], 'task_id': a['task_id']}
            for a in attempts if a.get('result') is not None]
    primary = [r for r in rows if r['attempt'] == 1]
    latest = {}
    for row in rows: latest[row['task_id']] = row
    planned = len(records) * config.repeats
    methods = {}
    for method in config.methods:
        method_rows = [r for r in primary if r['method_id'] == method]
        methods[method] = summary(method_rows, planned)
        methods[method]['by_type'] = {kind: summary([r for r in method_rows if r.get('type') == kind],
            sum(r['type'] == kind for r in records) * config.repeats) for kind in ('bridge', 'comparison')}
        methods[method]['by_repeat'] = {str(i): summary([r for r in method_rows if r['repeat'] == i], len(records))
                                       for i in range(config.repeats)}
        methods[method]['repeat_score_std'] = {}
        for metric in METRICS:
            values = [s['scores'][metric] for s in methods[method]['by_repeat'].values()]
            methods[method]['repeat_score_std'][metric] = (stdev(values) if len(values) > 1 and all(v is not None for v in values) else None)
    report = dict(schema_version=1, experiment_id=directory.name, purpose=config.purpose,
                  dataset_setting='HotpotQA dev distractor internal split', provenance=provenance,
                  scoring_source_sha256=source_fingerprint(), primary_policy='first_attempt',
                  bootstrap={'seed': config.seed, 'replicates': config.bootstrap_samples, 'unit': 'sample_mean_across_repeats'},
                  complete=all(m['complete'] for m in methods.values()), methods=methods,
                  pairs=comparisons(primary, config, [r['_id'] for r in records]),
                  retry_count=sum(r['attempt'] > 1 for r in rows),
                  all_attempt_costs=summary(rows, len(rows))['metrics'],
                  latest_attempt_supplement={m: summary([r for r in latest.values() if r['method_id'] == m], planned)
                                             for m in config.methods},
                  sessions=[dict(r) for r in ledger.db.execute('SELECT * FROM sessions')],
                  limitations=['Descriptive end-to-end comparison; not isolated causal attribution.',
                    'Development results cannot serve as untouched test evidence.',
                    'Bootstrap intervals resample questions, not provider/model uncertainty.',
                    'Unknown usage is not zero; interrupted requests may have unreported charges.'])
    if config.price:
        prompt = report['all_attempt_costs']['prompt_tokens']
        output = report['all_attempt_costs']['completion_tokens']
        report['cost_estimate'] = dict(**config.price.model_dump(),
            amount=((prompt['reported_total'] or 0) * config.price.input_per_million +
                    (output['reported_total'] or 0) * config.price.output_per_million) / 1e6
                    if prompt['reported'] or output['reported'] else None,
            usage_complete=bool(rows) and prompt['reported'] == output['reported'] == len(rows),
            note='Simple user-supplied input/output rates; excludes unreported usage and cache/reasoning price differences.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = directory / 'reports' / stamp
    target.mkdir(parents=True, exist_ok=False)
    atomic_json(target / 'summary.json', report)
    atomic_json(directory / 'reports' / 'latest.json', {'path': str(target.relative_to(directory)), 'sha256': digest(report)})
    with (directory / 'results.jsonl').open('w', encoding='utf-8') as handle:
        for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    write_csv(target / 'per-question.csv', ['sample_id', 'method_id', 'repeat', 'attempt', 'run_id', 'status',
        'answer', 'gold', 'scores', 'metrics', 'labels', 'retrieval'], rows)
    paired = []
    primary_index = {(r['sample_id'], r['method_id'], r['repeat']): r for r in primary}
    for record in records:
        for repeat in range(config.repeats):
            for left, right in combinations(config.methods, 2):
                a = primary_index.get((record['_id'], left, repeat), {})
                b = primary_index.get((record['_id'], right, repeat), {})
                valid = all(r.get('status') in ('completed', 'failed') for r in (a, b))
                paired.append(dict(sample_id=record['_id'], type=record['type'], repeat=repeat,
                    left=left, right=right, left_run=a.get('run_id'), right_run=b.get('run_id'),
                    left_status=a.get('status', 'unstarted'), right_status=b.get('status', 'unstarted'),
                    **{m + '_delta': b['scores'][m] - a['scores'][m] if valid else None for m in METRICS}))
    write_csv(target / 'paired-differences.csv', ['sample_id', 'type', 'repeat', 'left', 'right',
        'left_run', 'right_run', 'left_status', 'right_status', *[m + '_delta' for m in METRICS]], paired)
    # Never overwrite a reviewer's annotations: each generation gets its own directory.
    review = []
    for row in primary:
        review.append({**row, 'related_events': f'runs/{row["run_id"]}.json',
                       'human_label': '', 'human_confidence': '', 'human_notes': ''})
    write_csv(target / 'review.csv', ['sample_id', 'method_id', 'repeat', 'run_id', 'question', 'answer',
        'gold', 'supporting_evidence', 'scores', 'labels', 'related_events', 'human_label', 'human_confidence', 'human_notes'], review)
    examples = {}
    for label in sorted({label for r in primary for label in r.get('labels', [])}):
        examples[label] = sorted([r for r in primary if label in r.get('labels', [])],
            key=lambda r: digest([config.seed, label, r['sample_id'], r['method_id'], r['repeat']]))[:config.review_examples_per_category]
    atomic_json(target / 'examples.json', examples)
    for method in config.methods:
        for repeat in range(config.repeats):
            subset = [r for r in primary if r['method_id'] == method and r['repeat'] == repeat]
            by_id = {r['sample_id']: r for r in subset}
            predictions = {'answer': {}, 'sp': {}}
            for record in records:
                row = by_id.get(record['_id'], {})
                ok = row.get('status') == 'completed'
                predictions['answer'][record['_id']] = row.get('answer', '') if ok else ''
                predictions['sp'][record['_id']] = row.get('predicted_supporting_facts', []) if ok else []
            atomic_json(target / 'predictions' / f'{method}-r{repeat}.json', predictions)
    # Gold stays in the report directory, never in a Runtime context.
    atomic_json(target / 'gold.json', records)
    def number(value): return '—' if value is None else f'{value:.4f}'
    lines = ['# 六组基线实验报告', '', f'- 状态：{"完整" if report["complete"] else "未完成（不可作为正式完整比较）"}',
        f'- 用途：{config.purpose}；每方法计划 {planned} 次；首次尝试为主表。',
        '- observed_scores 仅为已产生评分的部分样本均值；主表不完整时不报告完整集成绩。',
        '', '| 方法 | 已尝试/计划 | EM | F1 | SP F1 | Joint F1 |', '| --- | ---: | ---: | ---: | ---: | ---: |']
    for method, values in methods.items():
        lines.append(f'| {method} | {values["attempted"]}/{planned} | ' + ' | '.join(number(values['scores'][k]) for k in ('em', 'f1', 'sp_f1', 'joint_f1')) + ' |')
    lines += ['', '## 开销与错误分析', '',
        '完整用量、延迟 mean/median/P95、分题型、分重复、触限标签与配对区间见 summary.json。',
        '补跑不替换主表；latest_attempt_supplement 单独呈现，all_attempt_costs 包含全部尝试。',
        '审阅 review.csv 并填写人工标签；自动标签只是观察，不能直接证明根因。',
        '如果协议之间检索量或模型调用量不同，本报告是端到端质量与成本比较。',
        '价格估算仅使用用户配置；未知用量不记零。温度 0 不保证确定性。',
        '', '## 人工研究记录', '', '- 观察：待填写。', '- 原因假设：待填写。', '- 后续验证：待填写。']
    (target / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return target
