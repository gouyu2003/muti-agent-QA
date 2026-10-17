"""Validate completed V0/V1 experiments and produce a reviewable comparison.

Run from the repository root:
python scripts/stage4_report.py --v0 data/experiments/stage4/smoke-v0-01 \
    --v1 data/experiments/stage4/smoke-v1-01 --output docs
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def load_experiment(path):
    rows = [json.loads(line) for line in (path / 'results.jsonl').read_text(encoding='utf-8').splitlines()]
    if len(rows) != 10 or len({row['sample_id'] for row in rows}) != 10:
        raise ValueError('Expected ten distinct terminal sample records')
    for row in rows:
        if row['status'] not in ('completed', 'failed'):
            raise ValueError('All ten samples must have a completed or failed terminal record')
        if row['status'] == 'failed' and (row['metrics'].get('total_tokens') is None
                or not row['metrics'].get('llm_call_count')):
            raise ValueError('Possible model invocation failure; preserve it and rerun in a new directory')
    if len({row['code_version'] for row in rows}) != 1:
        raise ValueError('Runtime source changed during the experiment')
    return rows, read_json(path / 'summary.json')


def observations(row, run_detail):
    tags = []
    if row['status'] == 'failed':
        tags.append('runtime_failure_after_model_responses')
    state = run_detail['run']['final_state'] or {}
    gold_facts = set(map(tuple, row['gold']['supporting_facts']))
    retrieved = {(chunk['source_title'], chunk.get('source_paragraph'))
                 for group in state.get('evidence_chunks', []) for chunk in group['chunks']}
    if not gold_facts <= retrieved:
        tags.append('gold_support_not_fully_retrieved')
    if row['scores']['em'] == 0:
        tags.append('answer_mismatch' if row['scores']['f1'] == 0 else 'partial_answer_or_format')
    if not row['verified']:
        tags.append('validator_rejection')
    elif row['scores']['f1'] == 0:
        tags.append('verified_answer_mismatch')
    if row['scores']['sp_em'] == 0:
        tags.append('supporting_fact_mismatch')
    return tags


def build_report(v0, v1, output):
    old, old_summary = load_experiment(v0)
    new, new_summary = load_experiment(v1)
    manifest = read_json(v0 / 'manifest.json')
    if manifest != read_json(v1 / 'manifest.json'):
        raise ValueError('Experiments must use the same manifest and dataset hash')
    old_by_id = {row['sample_id']: row for row in old}
    if set(old_by_id) != {row['sample_id'] for row in new}:
        raise ValueError('Sample IDs differ')
    details = {}
    for label, path, rows in [('v0', v0, old), ('v1', v1, new)]:
        details[label] = {row['sample_id']: read_json(path / 'runs' / f"{row['run_id']}.json") for row in rows}
    first = new[0]['sample_id']
    a = details['v0'][first]['run']['context']['config_snapshot']
    b = details['v1'][first]['run']['context']['config_snapshot']
    for agent_id in a['agents']:
        params_a = {key: value for key, value in a['agents'][agent_id]['model_config'].items() if key != 'system_prompt'}
        params_b = {key: value for key, value in b['agents'][agent_id]['model_config'].items() if key != 'system_prompt'}
        if params_a != params_b:
            raise ValueError(f'Model settings changed for {agent_id}')
    if a['workflow_spec']['max_cycles'] != b['workflow_spec']['max_cycles']:
        raise ValueError('Retry limits differ')
    for snapshot in (a, b):
        if snapshot['agents']['retriever_v0'].get('retrieval_config', {}).get('top_k', 3) != 3:
            raise ValueError('Smoke comparison requires top_k=3')
    compared = []
    for row in new:
        previous = old_by_id[row['sample_id']]
        if previous['gold'] != row['gold']:
            raise ValueError('Gold labels differ')
        compared.append({'sample_id': row['sample_id'], 'question': row['question'], 'type': row['type'],
            'gold_answer': row['gold']['answer'],
            'v0': {**previous, 'observations': observations(previous, details['v0'][row['sample_id']])},
            'v1': {**row, 'observations': observations(row, details['v1'][row['sample_id']])}})
    counts = {label: dict(Counter(tag for item in compared for tag in item[label]['observations'])) for label in ('v0', 'v1')}
    report = {'manifest': manifest, 'v0': old_summary, 'v1': new_summary,
              'observation_counts': counts, 'samples': compared,
              'samples_with_final_citations': {
                  'v0': sum(bool(row['supporting_evidence']) for row in old),
                  'v1': sum(bool(row['supporting_evidence']) for row in new)},
        'limitations': 'Ten development samples; observations are diagnostics, not proven causal attribution or statistical significance. Each experiment is sequential internally, but execution windows overlap; latency is exploratory and affected by shared remote service load.'}
    output.mkdir(parents=True, exist_ok=True)
    (output / 'stage4-smoke-comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# 阶段 4.0 Smoke-10 对照', '',
        '固定 5 道 bridge、5 道 comparison；模型参数、top_k 和协议重试上限保持一致。', '',
        '| 指标 | V0 | V1 |', '| --- | ---: | ---: |']
    for key in ('completion_rate', 'em', 'f1', 'sp_em', 'sp_f1', 'verification_pass_rate',
                'total_tokens_total', 'llm_call_count_total', 'duration_ms_total'):
        lines.append(f"| {key} | {old_summary[key]} | {new_summary[key]} |")
    lines += ['', '## 逐题对照', '', '| ID | 标准答案 | V0 答案 | V1 答案 | EM V0→V1 | F1 V0→V1 |', '| --- | --- | --- | --- | --- | --- |']
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    for item in compared:
        previous, current = item['v0'], item['v1']
        lines.append(f"| {item['sample_id']} | {cell(item['gold_answer'])} | {cell(previous['answer'])} | {cell(current['answer'])} | {previous['scores']['em']}→{current['scores']['em']} | {previous['scores']['f1']:.3f}→{current['scores']['f1']:.3f} |")
    lines += ['', '## 诊断观察', '', '以下类别根据输出与标准标签计算，可以重叠，不直接证明错误原因。', '']
    for label in ('v0', 'v1'):
        lines.append(f'- {label.upper()}: ' + json.dumps(counts[label], ensure_ascii=False))
    if not any(row['supporting_evidence'] for row in old):
        lines += ['', '本轮 V0 的最终支撑引用全部为空，无法执行坐标恢复，故支撑事实指标为零。这反映最终引用契约的缺口，不代表检索器没有命中证据；V1 的证据指标变化也包含引用回填修复的贡献。']
    lines += ['', '完整逐题引用、run_id、配置哈希、恢复的 V0 坐标和诊断标签见同目录 JSON。', '',
        '样本来自开发集，仅用于先导分析；verified 是内部判断，与标准答案正确率分开统计。不能据此宣称统计显著或泛化提升。', '',
        '每个实验内部逐题串行，两版实验的部分运行时间重叠。耗时受远端共享服务负载影响，仅作探索性记录；后续正式延迟比较应隔离执行并重复测量。', '']
    (output / 'stage4-smoke-comparison.md').write_text('\n'.join(lines), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--v0', type=Path, required=True)
    parser.add_argument('--v1', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build_report(args.v0, args.v1, args.output)
    print(json.dumps({'v0': result['v0'], 'v1': result['v1']}, indent=2))
