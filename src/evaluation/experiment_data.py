"""Versioned sampling independent of labels; the legacy Smoke-10 stays untouched."""
from __future__ import annotations

from pathlib import Path

from src.evaluation.experiment_config import ROOT, digest, read_json, atomic_json
from src.evaluation.hotpotqa import load_dataset, sha256_file
from src.knowledge.hotpotqa_context import runtime_input


def validate_dataset(path: Path):
    try:
        records = load_dataset(path)
    except (KeyError, TypeError) as exc:
        raise ValueError('Dataset must contain objects with unique string IDs') from exc
    for record in records:
        if not isinstance(record, dict) or not {'_id', 'question', 'context'} <= record.keys():
            raise ValueError('Dataset record is missing required Runtime fields')
        public = runtime_input(record)
        if len(public['question']) > 2000 or record.get('type') not in ('bridge', 'comparison'):
            raise ValueError('Invalid question length or HotpotQA type')
        if not isinstance(record.get('answer'), str) or not record['answer'].strip():
            raise ValueError('Missing answer label')
        facts = record.get('supporting_facts')
        paragraphs = dict(public['context'])
        if not isinstance(facts, list) or not facts:
            raise ValueError('Missing supporting_facts labels')
        for fact in facts:
            if (not isinstance(fact, list) or len(fact) != 2 or not isinstance(fact[0], str)
                    or type(fact[1]) is not int or fact[0] not in paragraphs
                    or not 0 <= fact[1] < len(paragraphs[fact[0]])):
                raise ValueError('Invalid supporting fact coordinates')
    return records


def sample(records, count, seed, purpose, strategy):
    if count > len(records):
        raise ValueError(f'Not enough eligible records for {purpose}: need {count}, have {len(records)}')
    groups = {kind: [r for r in records if r['type'] == kind] for kind in ('bridge', 'comparison')}
    weights = {k: len(v) if strategy == 'proportional' else 1 for k, v in groups.items()}
    total = sum(weights.values())
    quotas = {k: count * w // total for k, w in weights.items()}
    for key in sorted(groups, key=lambda k: (-(count * weights[k] % total), k))[:count - sum(quotas.values())]:
        quotas[key] += 1
    selected = []
    for key, group in groups.items():
        if quotas[key] > len(group):
            raise ValueError(f'Not enough {key} records for the requested sampling strategy')
        ordered = sorted(group, key=lambda r: digest([seed, purpose, r['_id']]))
        selected.extend(ordered[:quotas[key]])
    return sorted(selected, key=lambda r: digest([seed, purpose, 'order', r['_id']]))


def known_exposures(config):
    ids = set(config.exclude_evaluation_ids)
    old = ROOT / 'docs/stage4-smoke-10.json'
    if old.exists():
        ids.update(s['sample_id'] for s in read_json(old)['samples'])
    fixture = ROOT / 'data/hotpotqa/fixture_001.json'
    if fixture.exists():
        ids.add(read_json(fixture)['_id'])
    for path in config.exposure_manifests:
        manifest = read_json(Path(path))
        if manifest.get('purpose') not in ('development', 'smoke'):
            raise ValueError('Exposure manifests must be development or smoke manifests')
        ids.update(s['sample_id'] for s in manifest['samples'])
    return ids


def manifests(config, records):
    dataset_hash = sha256_file(Path(config.dataset))
    if config.dataset_sha256 and dataset_hash != config.dataset_sha256:
        raise ValueError('Dataset SHA-256 does not match configured value')
    dev = sample(records, config.splits.development, config.seed, 'development', config.sampling)
    excluded = known_exposures(config) | {r['_id'] for r in dev}
    evaluation = sample([r for r in records if r['_id'] not in excluded], config.splits.evaluation,
                        config.seed, 'evaluation', config.sampling)
    smoke = sample(dev, config.splits.smoke, config.seed, 'smoke', config.sampling)
    output = {}
    for purpose, selected in [('development', dev), ('evaluation', evaluation), ('smoke', smoke)]:
        manifest = dict(schema_version=1, selection_version='stage56-v1', dataset_sha256=dataset_hash,
                        dataset_records=len(records), duplicate_ids=0,
                        dataset_type_counts={k: sum(r['type'] == k for r in records) for k in ('bridge', 'comparison')},
                        source=config.dataset_source, purpose=purpose, seed=config.seed,
                        sampling=config.sampling, excluded_evaluation_ids=sorted(excluded),
                        samples=[{'sample_id': r['_id'], 'type': r['type']} for r in selected])
        manifest['sha256'] = digest(manifest)
        output[purpose] = manifest
    return output


def prepare_data(config):
    records = validate_dataset(Path(config.dataset))
    expected = manifests(config, records)
    directory = Path(config.split_directory)
    # Validate ALL existing files before writing any of them.
    for purpose, value in expected.items():
        path = directory / f'{purpose}.json'
        if path.exists() and read_json(path) != value:
            raise ValueError('Prepared split differs; use a new split directory and register prior exposures')
    for purpose, value in expected.items():
        path = directory / f'{purpose}.json'
        if not path.exists():
            atomic_json(path, value)
    return {key: len(value['samples']) for key, value in expected.items()}


def selected_data(config):
    records = validate_dataset(Path(config.dataset))
    expected = manifests(config, records)
    for purpose, value in expected.items():
        path = Path(config.split_directory) / f'{purpose}.json'
        if not path.exists() or read_json(path) != value:
            raise ValueError('Missing or changed prepared manifest; run prepare first')
    selected = expected[config.purpose]
    index = {r['_id']: r for r in records}
    return [index[s['sample_id']] for s in selected['samples']], selected
