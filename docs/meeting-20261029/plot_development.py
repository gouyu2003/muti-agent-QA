"""Render descriptive development figures from the verified analysis JSON."""
import argparse
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--skill-scripts', type=Path, required=True)
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
data = json.loads((BASE / 'development-analysis.json').read_text(encoding='utf-8'))
methods = list(data['methods'])
names = ['Single RAG', 'Fixed pipeline', 'Adaptive pipeline', 'Multi expert', 'Lead', 'Debate']
colors = ['#64748B', '#236B8E', '#2A817A', '#8793A1', '#A66C53', '#88639C']
plt.rcParams.update({'font.family':'sans-serif', 'font.sans-serif':['Arial','Helvetica','DejaVu Sans'], 'font.size':12, 'axes.labelsize':13,
    'axes.spines.top':False, 'axes.spines.right':False, 'axes.linewidth':.8,
    'svg.fonttype':'none', 'pdf.fonttype':42, 'figure.facecolor':'white', 'axes.facecolor':'white'})

def save(fig, stem):
    fig.canvas.draw()
    require_matplotlib_panel_alignment(fig, json_out=str(OUT / (stem+'.alignment.json')),
        tolerance_pt=1.5, strict=True)
    fig.savefig(OUT / f'{stem}.svg')
    fig.savefig(OUT / f'{stem}.pdf')
    fig.savefig(OUT / f'{stem}.png', dpi=300)
    plt.close(fig)

f1 = np.array([data['methods'][m]['scores']['f1']*100 for m in methods])
fig, ax = plt.subplots(figsize=(10,5.8))
fig.subplots_adjust(left=.10,right=.97,bottom=.23,top=.80)
ax.bar(np.arange(6),f1,color=colors,width=.60)
for i, value in enumerate(f1):
    ax.text(i,value+1.8,f'{value:.2f}',ha='center',va='bottom',fontsize=13)
ax.set_xticks(np.arange(6),['Single\nRAG','Fixed\npipeline','Adaptive\npipeline','Multi\nexpert','Lead','Debate'])
ax.set_ylim(0,82); ax.set_yticks([0,20,40,60,80]); ax.set_ylabel('Answer F1 (0-100)')
ax.set_axisbelow(True)
fig.text(.035,.94,'Development: modest pipelines lead the quality ranking',fontsize=17,weight='bold')
fig.text(.035,.88,'100 shared questions per method; each method run once',fontsize=12,color='#475569')
fig.text(.035,.055,'Descriptive means; failed tasks score zero. No across-run error bars: there was one run per method.',fontsize=10)
save(fig,'01_answer_quality')

fig, ax = plt.subplots(figsize=(10,5.8))
fig.subplots_adjust(left=.10,right=.97,bottom=.20,top=.80)
calls = [data['methods'][m]['mean_calls'] for m in methods]
offsets = [(10,-22),(-8,15),(12,8),(10,-24),(12,0),(-10,14)]
for x,y,c,name,offset in zip(calls,f1,colors,names,offsets):
    ax.scatter(x,y,s=100,color=c,zorder=3)
    ax.annotate(name,(x,y),xytext=offset,textcoords='offset points',
        ha='right' if offset[0]<0 else 'left',va='center',fontsize=12)
ax.set_xlim(.4,11.3); ax.set_ylim(25,76)
ax.set_xlabel('Mean actual LLM calls per task'); ax.set_ylabel('Answer F1 (0-100)')
ax.set_axisbelow(True)
fig.text(.035,.94,'More collaboration did not yield higher quality in this run',fontsize=17,weight='bold')
fig.text(.035,.88,'Quality versus observed call count; shared maximum limits, different actual work',fontsize=12,color='#475569')
fig.text(.035,.055,'Calls include retries. This is a descriptive quality/call comparison, not a monetary cost or causal estimate.',fontsize=10)
save(fig,'02_quality_calls')

pairs = [p for p in data['paired'] if p['left']=='single_rag']
assert len(pairs)==5
fig, ax = plt.subplots(figsize=(10,5.8))
fig.subplots_adjust(left=.23,right=.65,bottom=.22,top=.80)
for i,p in enumerate(pairs):
    result=p['metrics']['f1']; value=result['delta']*100
    lo,hi=np.array(result['ci95_sample_bootstrap'])*100
    ax.errorbar(value,i,xerr=np.array([[value-lo],[hi-value]]),fmt='o',color=colors[i+1],
        markersize=7,capsize=4,elinewidth=1.7)
ax.axvline(0,color='#64748B',ls='--',lw=.9,zorder=0)
ax.set_yticks(np.arange(5),names[1:]); ax.set_ylim(4.7,-.7)
ax.set_xlim(-35,20); ax.set_xticks([-30,-20,-10,0,10,20]); ax.set_xlabel('Paired F1 difference (score points)')
fig.text(.035,.94,'Paired differences show substantial uncertainty',fontsize=17,weight='bold')
fig.text(.035,.88,'Each method minus Single RAG; exploratory 95% question-bootstrap intervals',fontsize=12,color='#475569')
fig.canvas.draw()
for i,p in enumerate(pairs):
    result=p['metrics']['f1']; lo,hi=np.array(result['ci95_sample_bootstrap'])*100
    y=fig.transFigure.inverted().transform(ax.transData.transform((0,i)))[1]
    fig.text(.72,y,f"{result['delta']*100:+.2f} [{lo:+.2f}, {hi:+.2f}]",va='center',fontsize=12)
fig.text(.035,.07,'n = 100 paired questions; 2,000 bootstrap resamples; one model run per method.',fontsize=10)
fig.text(.035,.035,'Intervals are unadjusted and describe question resampling, not model-run variability.',fontsize=10)
save(fig,'03_paired_f1')
print(json.dumps({'figures':3,'formats':['png','pdf','svg'],'questions':100,'backend':'python'}))
