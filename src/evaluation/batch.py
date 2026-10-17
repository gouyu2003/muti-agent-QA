"""Sequential, isolated HotpotQA experiments using the production RunService.

python -m src.evaluation.batch select --dataset ... --manifest ...
python -m src.evaluation.batch run --dataset ... --manifest ... --output ...
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import zipfile

from src.evaluation.hotpotqa import (evidence_facts, load_dataset, make_manifest,
    runtime_input, select_records, sha256_file, write_json)
from src.evaluation.metrics import score
from src.runtime.models import RunContext
from src.runtime.service import RunService
from src.runtime.specs import KnowledgeSourceSpec
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.system import resolve_system

ROOT = Path(__file__).resolve().parents[2]


def build_batch_context(base, record: dict, source_path: Path, experiment_id: str) -> RunContext:
    public = runtime_input(record)
    write_json(source_path, public)
    source = KnowledgeSourceSpec(source_id=f"sample_{public['_id']}", source_type='hotpotqa_context',
                               path=source_path.name, name='HotpotQA isolated context')
    system = replace(base, source=source, source_path=source_path.resolve(),
                     source_digest=sha256_file(source_path), record=public, source_metadata=None)
    # The immutable replacement supplies only this sample's public context.
    from src.runtime.version import code_version
    return RunContext(run_id=str(uuid4()), entrypoint='batch', question=public['question'],
                      source_id=source.source_id, record_id=public['_id'],
                      workflow_id=base.workflow.protocol_id, method_id=base.workflow.method_id,
                      model_name=next(iter(base.agents.values())).config['model_config']['model_name'],
                      protocol_id=base.workflow.protocol_id, code_version=code_version(),
                      config_snapshot=system.snapshot(), experiment_id=experiment_id, sample_id=public['_id'])


def summarize(results: list[dict]) -> dict:
    count = len(results)
    summary = {'sample_count': count,
               'completed': sum(row['status'] == 'completed' for row in results),
               'verified': sum(row['verified'] for row in results)}
    summary['completion_rate'] = summary['completed'] / count if count else 0.0
    summary['verification_pass_rate'] = summary['verified'] / summary['completed'] if summary['completed'] else 0.0
    for name in ('em', 'f1', 'sp_em', 'sp_f1'):
        summary[name] = sum(row['scores'][name] for row in results) / count if count else 0.0
    for name in ('total_tokens', 'prompt_tokens', 'completion_tokens', 'llm_call_count', 'duration_ms'):
        values = [row['metrics'].get(name) for row in results]
        present = [value for value in values if value is not None]
        summary[f'{name}_total'] = sum(present) if present else None
        summary[f'{name}_mean'] = sum(present) / len(present) if present else None
        summary[f'{name}_reported_samples'] = len(present)
    return summary


def run_batch(dataset: Path, manifest_path: Path, output: Path, *, service=None) -> dict:
    records, manifest = select_records(dataset, manifest_path)
    # Never overwrite a previous experiment, including failures.
    output.mkdir(parents=True, exist_ok=False)
    experiment_id = output.name
    base = resolve_system()
    service = service or RunService(SQLiteRunRepository(output / 'runs.sqlite3'))
    write_json(output / 'manifest.json', manifest)
    results = []
    for record in records:
        sample_id = record['_id']
        filename = hashlib.sha256(sample_id.encode('utf-8')).hexdigest() + '.json'
        context = build_batch_context(base, record, output / 'contexts' / filename, experiment_id)
        detail = service.run(context)
        write_json(output / 'runs' / f'{context.run_id}.json', detail.model_dump(mode='json'))
        run = detail.run
        state = run.final_state or {}
        items = state.get('supporting_evidence', [])
        facts, recovered = evidence_facts(items, record['context'])
        scores = score(state.get('answer', ''), facts, record)
        if run.status != 'completed':
            scores = dict.fromkeys(scores, 0.0)
        row = {'sample_id': sample_id, 'question': record['question'], 'type': record['type'],
               'run_id': context.run_id, 'experiment_id': experiment_id,
               'method_id': context.method_id, 'code_version': context.code_version,
               'config_sha256': context.config_snapshot['config_sha256'],
               'status': run.status, 'answer': state.get('answer', ''),
               'verified': bool(state.get('verified')) and run.status == 'completed',
               'supporting_evidence': items, 'predicted_supporting_facts': facts,
               'recovered_v0_coordinates': recovered, 'scores': scores,
               'metrics': run.metrics.model_dump(), 'error': run.error.model_dump() if run.error else None,
               'verification_note': state.get('verification_note', ''),
               'gold': {'answer': record['answer'], 'supporting_facts': record['supporting_facts']}}
        results.append(row)
        with (output / 'results.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        write_json(output / 'summary.json', summarize(results))
        print(f"{len(results)}/{len(records)} {sample_id}: {run.status} EM={scores['em']} F1={scores['f1']:.3f}", flush=True)
    predictions = {'answer': {row['sample_id']: row['answer'] if row['status'] == 'completed' else '' for row in results},
                   'sp': {row['sample_id']: row['predicted_supporting_facts'] if row['status'] == 'completed' else [] for row in results}}
    write_json(output / 'predictions.json', predictions)
    write_json(output / 'gold.json', records)
    return summarize(results)


def freeze(output: Path) -> None:
    """Explicit tracked sources plus the new evaluation package; never .env/user data."""
    output.mkdir(parents=True, exist_ok=False)
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    paths = {ROOT / name for name in tracked if name and not name.startswith('data/experiments/')}
    paths.update((ROOT / 'src' / 'evaluation').glob('*.py'))
    paths.add(ROOT / 'src' / 'knowledge' / 'hotpotqa_context.py')
    paths.update((ROOT / 'scripts').glob('stage4*.py'))
    paths.update((ROOT / 'tests').glob('test_stage4*.py'))
    paths.add(ROOT / 'tests' / 'test_evaluation.py')
    paths = {path for path in paths if path.is_file() and path.name != '.env'}
    hashes = {}
    with zipfile.ZipFile(output / 'source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            relative = path.relative_to(ROOT).as_posix()
            archive.write(path, relative)
            hashes[relative] = sha256_file(path)
    from src.runtime.version import code_version
    write_json(output / 'baseline.json', {
        'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
        'code_version': code_version(), 'snapshot': resolve_system().snapshot(),
        'archive_sha256': sha256_file(output / 'source.zip'), 'files': hashes})
    (output / 'dependencies.txt').write_text(subprocess.check_output(
        [sys.executable, '-m', 'pip', 'freeze'], text=True), encoding='utf-8')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('select', 'run'):
        part = sub.add_parser(command)
        part.add_argument('--dataset', type=Path, required=True)
        part.add_argument('--manifest', type=Path, required=True)
        if command == 'run':
            part.add_argument('--output', type=Path, required=True)
    sub.add_parser('freeze').add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'select':
        if args.manifest.exists():
            raise ValueError('Manifest already exists; do not replace a fixed sample list')
        write_json(args.manifest, make_manifest(load_dataset(args.dataset), sha256_file(args.dataset)))
    elif args.command == 'freeze':
        freeze(args.output)
    else:
        from dotenv import load_dotenv
        load_dotenv()
        print(json.dumps(run_batch(args.dataset, args.manifest, args.output), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
