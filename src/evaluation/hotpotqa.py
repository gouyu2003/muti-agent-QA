"""Deterministic sample selection and a context-only Runtime boundary."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.knowledge.hotpotqa_loader import build_paragraph_index
from src.knowledge.hotpotqa_context import runtime_input, load_context

SELECTION_VERSION = 'stage4-smoke-v1'


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def load_dataset(path: Path) -> list[dict]:
    records = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(records, list) or not records:
        raise ValueError('HotpotQA dataset must be a nonempty JSON array')
    ids = [record['_id'] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate sample IDs')
    return records


def make_manifest(records: list[dict], dataset_sha256: str) -> dict:
    selected = []
    for kind in ('bridge', 'comparison'):
        candidates = [record for record in records if record.get('type') == kind]
        candidates.sort(key=lambda record: hashlib.sha256(
            f"{SELECTION_VERSION}:{record['_id']}".encode()).hexdigest())
        if len(candidates) < 5:
            raise ValueError(f'At least five {kind} records are required')
        selected.extend({'sample_id': record['_id'], 'type': kind} for record in candidates[:5])
    return {'selection_version': SELECTION_VERSION, 'dataset_sha256': dataset_sha256, 'samples': selected}


def select_records(path: Path, manifest_path: Path) -> tuple[list[dict], dict]:
    records = load_dataset(path)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest != make_manifest(records, sha256_file(path)):
        raise ValueError('Manifest does not match the fixed selection or dataset hash')
    by_id = {record['_id']: record for record in records}
    return [by_id[item['sample_id']] for item in manifest['samples']], manifest


def evidence_facts(items: list[dict], context: list) -> tuple[list, list]:
    """Recover V0 coordinates only from unique exact text within its source title."""
    paragraphs = build_paragraph_index(context)
    facts, recovered = [], []
    for item in items:
        title = item.get('source_title')
        sentences = paragraphs.get(title, [])
        index = item.get('source_paragraph')
        if index is None:
            matches = [i for i, text in enumerate(sentences) if text == item.get('text')]
            if len(matches) != 1:
                continue
            index = matches[0]
            recovered.append([title, index])
        if (type(index) is int and 0 <= index < len(sentences)
                and sentences[index] == item.get('text') and [title, index] not in facts):
            facts.append([title, index])
    return facts, recovered
