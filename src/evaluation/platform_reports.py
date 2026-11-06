"""Research summaries for arbitrary published systems, using the shared paired estimator."""
import csv
import io
import json
from statistics import stdev
from types import SimpleNamespace

from src.evaluation.experiment_config import digest, atomic_json
from src.evaluation.experiment_report import comparisons
from src.evaluation.experiment_scores import METRICS


def relative_pairs(pairs, reference):
    result = []
    for pair in pairs:
        if pair['left'] == reference:
            result.append(pair)
        elif pair['right'] == reference:
            metrics = {key: (dict(delta=-value['delta'],
                ci95_sample_bootstrap=[-value['ci95_sample_bootstrap'][1], -value['ci95_sample_bootstrap'][0]],
                left_wins=value['right_wins'], right_wins=value['left_wins'], ties=value['ties']) if value else None)
                for key, value in pair['metrics'].items()}
            result.append({**pair, 'left': reference, 'right': pair['left'], 'metrics': metrics})
    return result


def research_report(frozen, primary, aggregate, directory):
    request = frozen['request']
    records = frozen['records']
    repeats = frozen['repeats']
    types = {r['_id']: r['type'] for r in records}
    rows = [{**r, 'type': types[r['sample_id']]} for r in primary]
    methods = {}
    for method in frozen['definitions']:
        own = [r for r in rows if r['method_id'] == method]
        by_type = {kind: aggregate([r for r in own if r['type'] == kind],
            sum(r['type'] == kind for r in records) * repeats, 0) for kind in sorted(set(types.values()))}
        by_repeat = {str(i + 1): aggregate([r for r in own if r['repeat'] == i], len(records), 0) for i in range(repeats)}
        deviations = {}
        for metric in METRICS:
            values = [s['scores'][metric]['value'] for s in by_repeat.values()]
            deviations[metric] = stdev(values) if len(values) > 1 and all(v is not None for v in values) else None
        methods[method] = dict(by_type=by_type, by_repeat=by_repeat, repeat_score_std=deviations)
    complete = len(rows) == len(records) * len(methods) * repeats and all(r['status'] in ('completed', 'failed') for r in rows)
    config = SimpleNamespace(methods=list(methods), repeats=repeats, seed=request.get('seed', 42),
                             bootstrap_samples=request.get('bootstrap_samples', 2000))
    pairs = []
    if complete:
        key = digest([rows, config.__dict__])
        cache = directory / 'paired-cache.json'
        cached = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
        if cached.get('key') == key:
            pairs = cached['pairs']
        else:
            pairs = comparisons(rows, config, [r['_id'] for r in records])
            atomic_json(cache, dict(key=key, pairs=pairs))
    reference = frozen.get('reference_method', next(iter(methods)))
    return dict(complete=complete, primary_metric=request.get('primary_metric', 'f1'), methods=methods,
                reference_method=reference, pairs=pairs, reference_pairs=relative_pairs(pairs, reference),
                bootstrap=dict(seed=config.seed, replicates=config.bootstrap_samples,
                               unit='同题先对重复运行取均值，再按题重采样', confidence=0.95),
                notes=['差值为右侧方法减左侧方法；只使用首次尝试。',
                       '缺少标准答案或支撑事实的指标不计算配对区间；不将未知记为零。',
                       '区间反映按题抽样的不确定性，不代表模型重复随机性；多重比较未校正。'])


def paired_csv(job):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['analysis', 'left', 'right', 'metric', 'paired_samples', 'delta_right_minus_left', 'ci95_low', 'ci95_high', 'left_wins', 'ties', 'right_wins'])
    research = job['report']['research']
    primary = relative_pairs(research['pairs'], research['reference_method'])
    others = [p for p in research['pairs'] if research['reference_method'] not in (p['left'], p['right'])]
    for pair in primary + others:
        for metric, data in pair['metrics'].items():
            main = pair['left'] == research['reference_method'] and metric == research['primary_metric']
            writer.writerow(['primary' if main else 'secondary_exploratory', pair['left'], pair['right'], metric, pair['paired_samples'],
                *([data['delta'], *data['ci95_sample_bootstrap'], data['left_wins'], data['ties'], data['right_wins']]
                  if data else [None] * 6)])
    return '\ufeff' + output.getvalue()


def markdown_report(job):
    report = job['report']
    research = report['research']
    label = {t['method_id']: t['label'].replace('|', '\\|').replace('\n', ' ') for t in job['targets']}
    lines = ['# 对比实验报告', '', f"任务：{job['name']}", f"冻结指纹：{job['fingerprint']}",
             f"首次尝试完整：{research['complete']}", f"主指标：{research['primary_metric']}", '',
             '| 方法 | 完成 | 失败 | EM | F1 |', '|---|---:|---:|---:|---:|']
    def value(score):
        return '未知' if score is None else f'{score:.6f}'
    for method, data in report['methods'].items():
        lines.append(f"| {label[method]} | {data['statuses']['completed']} | {data['statuses']['failed']} | "
                     f"{value(data['scores']['em']['value'])} | {value(data['scores']['f1']['value'])} |")
    lines += ['', '## 主要比较：主指标相对指定基准', '', '| 基准 | 对象 | 指标 | 差值（对象−基准） | 95% 区间 |', '|---|---|---|---:|---|']
    for pair in relative_pairs(research['pairs'], research['reference_method']):
        metric = research['primary_metric']
        data = pair['metrics'][metric]
        if data:
            low, high = data['ci95_sample_bootstrap']
            lines.append(f"| {label[pair['left']]} | {label[pair['right']]} | {metric} | {data['delta']:.6f} | [{low:.6f}, {high:.6f}] |")
    lines += ['', '## 全部两两比较（含主要比较，其余为次要或探索性分析）', '', '| 左侧 | 右侧 | 指标 | 差值（右−左） | 95% 区间 |', '|---|---|---|---:|---|']
    for pair in research['pairs']:
        for metric, data in pair['metrics'].items():
            if data:
                low, high = data['ci95_sample_bootstrap']
                lines.append(f"| {label[pair['left']]} | {label[pair['right']]} | {metric} | {data['delta']:.6f} | [{low:.6f}, {high:.6f}] |")
    timing_rows = [(method, phase, item) for method, data in report['methods'].items()
                   for phase, item in data.get('timing_breakdown', {}).items()]
    if timing_rows:
        lines += ['', '## 耗时拆分', '', '| 方法 | 环节 | 平均经过/秒 | 已记录/尝试 |', '|---|---|---:|---:|']
        for method, phase, item in timing_rows:
            lines.append(f"| {label[method]} | {phase} | {item['mean_ms']/1000:.3f} | {item['reported']}/{item['attempted']} |")
        lines += ['', '缺失拆分不补零；环节可能嵌套，不可相加。']
    lines += ['', '## 统计说明', '', *research['notes'], '',
              '完整的题型/重复分层、开销分布、所有尝试开销和补跑补充见报告 JSON。',
              '未知用量不是零；Token 统计不等同于供应商实际账单。']
    return '\n'.join(lines) + '\n'
