"""Plot verified first-attempt evaluation statistics without accessing model services."""
import argparse
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--skill-scripts', type=Path, required=True)
parser.add_argument('--overview-only', action='store_true', help='Export only the Answer F1 overview.')
args = parser.parse_args()
sys.path.insert(0, str(args.skill_scripts))
from audit_panel_alignment import require_matplotlib_panel_alignment
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

BASE = Path(__file__).resolve().parent
OUT = BASE / 'figures'
OUT.mkdir(exist_ok=True)
data = json.loads((BASE / 'analysis.json').read_text(encoding='utf-8'))
methods = list(data['methods'])
names = ['Single RAG', 'Fixed pipeline', 'Adaptive pipeline', 'Multi expert', 'Lead', 'Debate']
colors = ['#64748B', '#236B8E', '#2A817A', '#8793A1', '#A66C53', '#88639C']
plt.rcParams.update({'font.family': 'sans-serif', 'font.sans-serif': ['Arial', 'Helvetica', 'DejaVu Sans'],
    'font.size': 12, 'axes.labelsize': 13, 'axes.spines.top': False, 'axes.spines.right': False,
    'axes.linewidth': .8, 'svg.fonttype': 'none', 'pdf.fonttype': 42,
    'figure.facecolor': 'white', 'axes.facecolor': 'white', 'legend.frameon': False})


def save(fig, stem):
    fig.canvas.draw()
    require_matplotlib_panel_alignment(fig, json_out=str(OUT / (stem + '.alignment.json')),
        tolerance_pt=1.5, strict=True)
    fig.savefig(OUT / f'{stem}.svg')
    fig.savefig(OUT / f'{stem}.pdf')
    fig.savefig(OUT / f'{stem}.png', dpi=300)
    # Keep generated path attributes unchanged while removing line-end whitespace.
    svg = OUT / f'{stem}.svg'
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text(encoding='utf-8').splitlines()) + '\n', encoding='utf-8')
    plt.close(fig)


# Descriptive comparison: all six frozen first-attempt means, including failures.
# Mapping: methods[method].scores.f1 -> bar height, multiplied by 100.
# One run per method: no invented across-run variability; paired intervals are separate.
assert methods == ['single_rag', 'pipeline_fixed', 'pipeline_adaptive', 'multi_expert', 'lead', 'debate']
assert data['questions'] == 1000 and data['tasks'] == 6000 and data['repeats'] == 1
overview_f1 = [data['methods'][method]['scores']['f1'] * 100 for method in methods]
assert all(np.isfinite(value) and 0 <= value <= 100 for value in overview_f1)
fig, ax = plt.subplots(figsize=(10, 5.8))
fig.subplots_adjust(left=.10, right=.97, bottom=.23, top=.79)
bars = ax.bar(np.arange(len(methods)), overview_f1, width=.60, color=colors)
for bar, value in zip(bars, overview_f1):
    ax.annotate(f'{value:.2f}', (bar.get_x() + bar.get_width() / 2, value),
                xytext=(0, 6), textcoords='offset points', ha='center', va='bottom', fontsize=12)
ax.set_xticks(np.arange(len(methods)), ['Single\nRAG', 'Fixed\npipeline', 'Adaptive\npipeline',
                                        'Multi\nexpert', 'Lead', 'Debate'])
ax.set_ylim(0, 82)
ax.set_yticks([0, 20, 40, 60, 80])
ax.set_ylabel('Answer F1 (0-100)')
fig.text(.035, .94, 'Evaluation: pipelines lead the quality ranking', fontsize=17, weight='bold')
fig.text(.035, .88, '1,000 shared questions per method; each method run once', fontsize=12, color='#475569')
fig.text(.035, .055, 'Descriptive means; failed tasks score zero. No across-run error bars: one run per method.', fontsize=10)
save(fig, '00_answer_f1_overview')
if args.overview_only:
    print(json.dumps({'figures': 1, 'questions': 1000, 'backend': 'python', 'figure': '00_answer_f1_overview'}))
    raise SystemExit(0)


pairs = [pair for pair in data['paired'] if pair['left'] == 'single_rag']
assert len(pairs) == 5 and all(pair['paired_samples'] == 1000 for pair in pairs)
fig, ax = plt.subplots(figsize=(10, 5.8))
fig.subplots_adjust(left=.23, right=.65, bottom=.22, top=.80)
for index, pair in enumerate(pairs):
    result = pair['metrics']['f1']
    value = result['delta'] * 100
    lo, hi = np.array(result['ci95_sample_bootstrap']) * 100
    assert lo <= value <= hi
    ax.errorbar(value, index, xerr=np.array([[value - lo], [hi - value]]), fmt='o', color=colors[index + 1],
        markersize=7, capsize=4, elinewidth=1.7)
ax.axvline(0, color='#64748B', ls='--', lw=.9, zorder=0)
ax.set_yticks(np.arange(5), names[1:])
ax.set_ylim(4.7, -.7)
ax.set_xlim(-26, 10)
ax.set_xticks([-20, -10, 0, 10])
ax.set_xlabel('Paired Answer F1 difference (score points)')
fig.text(.035, .94, 'Evaluation: pipelines gain Answer F1 over Single RAG', fontsize=17, weight='bold')
fig.text(.035, .88, 'Each method minus Single RAG; stored 95% question-bootstrap intervals', fontsize=12, color='#475569')
fig.canvas.draw()
for index, pair in enumerate(pairs):
    result = pair['metrics']['f1']
    lo, hi = np.array(result['ci95_sample_bootstrap']) * 100
    y = fig.transFigure.inverted().transform(ax.transData.transform((0, index)))[1]
    fig.text(.72, y, f"{result['delta'] * 100:+.2f} [{lo:+.2f}, {hi:+.2f}]", va='center', fontsize=12)
fig.text(.035, .07, 'n = 1,000 paired questions; 2,000 bootstrap resamples; failures remain in the denominator.', fontsize=10)
fig.text(.035, .035, 'Intervals are unadjusted. They describe question resampling, not repeated provider/model runs.', fontsize=10)
save(fig, '01_paired_answer_f1')

fig, ax = plt.subplots(figsize=(10, 5.8))
fig.subplots_adjust(left=.10, right=.97, bottom=.20, top=.80)
f1 = [data['methods'][method]['scores']['f1'] * 100 for method in methods]
calls = [data['methods'][method]['metrics']['llm_call_count']['mean_reported'] for method in methods]
offsets = [(10, -20), (-8, 18), (12, 10), (10, -20), (12, 0), (-10, 14)]
for x, y, color, name, offset in zip(calls, f1, colors, names, offsets):
    ax.scatter(x, y, s=100, color=color, zorder=3)
    ax.annotate(name, (x, y), xytext=offset, textcoords='offset points',
        ha='right' if offset[0] < 0 else 'left', va='center', fontsize=12)
ax.set_xlim(.4, 11.3)
ax.set_ylim(27, 72)
ax.set_xlabel('Mean actual LLM calls per task')
ax.set_ylabel('Answer F1 (0-100)')
fig.text(.035, .94, 'Small pipelines offer a favorable quality/call tradeoff', fontsize=17, weight='bold')
fig.text(.035, .88, 'Same 1,000 questions per method; shared ceilings, different actual work', fontsize=12, color='#475569')
fig.text(.035, .055, 'Calls include retries. This comparison does not isolate topology or measure monetary charges.', fontsize=10)
save(fig, '02_quality_calls')

categories = [('protocol_timeout', 'Task timeout', '#527A9A'),
              ('debate_output_validation', 'Revision validation', '#9172AC'),
              ('content_filter', 'Content filter', '#C18445')]
fig, ax = plt.subplots(figsize=(10, 5.8))
fig.subplots_adjust(left=.23, right=.93, bottom=.19, top=.74)
left = np.zeros(len(methods))
for key, label, color in categories:
    counts = np.array([data['methods'][method]['failures'].get(key, 0) for method in methods])
    ax.barh(np.arange(len(methods)), counts, left=left, color=color, height=.58, label=label)
    left += counts
assert sum(left) == 92
for index, count in enumerate(left):
    ax.text(count + .8, index, f'{int(count)}', va='center', fontsize=12)
ax.set_yticks(np.arange(len(methods)), names)
ax.set_ylim(5.6, -.6)
ax.set_xlim(0, 58)
ax.set_xticks([0, 10, 20, 30, 40, 50])
ax.set_xlabel('Failed first-attempt tasks (1,000 planned per method)')
ax.legend(loc='lower left', bbox_to_anchor=(0, 1.02), ncol=3, borderaxespad=0, fontsize=11,
          handlelength=1.2, columnspacing=1.5)
fig.text(.035, .94, 'Most failed tasks were timeouts or invalid revision messages', fontsize=17, weight='bold')
fig.text(.035, .88, '92 failed tasks: 55 timeouts, 25 revision validation failures, 12 content filters', fontsize=12, color='#475569')
fig.text(.035, .055, 'Counts are terminal task outcomes. Transient request errors that recovered are reported separately.', fontsize=10)
save(fig, '03_failure_breakdown')
print(json.dumps({'figures': 4, 'questions': 1000, 'backend': 'python'}))
