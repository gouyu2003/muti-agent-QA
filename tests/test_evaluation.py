from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.evaluation.batch import build_batch_context, run_batch, summarize
from src.evaluation.hotpotqa import (evidence_facts, load_context, make_manifest,
                                    runtime_input, sha256_file, write_json)
from src.evaluation.metrics import answer_scores, supporting_scores
from src.runtime.models import RunDetail, RunMetrics, RunRecord
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.runtime.system import resolve_system, retriever_from_snapshot


def record(index=0, kind='bridge'):
    return {'_id': f'id{index}', 'question': 'Where is Cedar Institute?', 'type': kind,
            'answer': 'GOLD_ONLY_LABEL', 'supporting_facts': [['Cedar', 0]],
            'context': [['Cedar', ['Cedar Institute is in Northbridge.']],
                        ['Other', ['Rain falls in winter.', 'Trees grow in spring.']]]}


def test_runtime_boundary_and_metadata_roundtrip(tmp_path):
    context = build_batch_context(resolve_system(), record(), tmp_path / 'context.json', 'exp-1')
    public = load_context(tmp_path / 'context.json')
    assert set(public) == {'_id', 'question', 'context'}
    assert 'GOLD_ONLY_LABEL' not in json.dumps(context.model_dump())
    retriever = retriever_from_snapshot(context.config_snapshot)
    assert all(item['source_title'] == 'Cedar' for item in retriever.search('Cedar'))
    repository = SQLiteRunRepository(tmp_path / 'runs.sqlite3')
    repository.initialize()
    from datetime import datetime, timezone
    run = RunRecord(context=context, status='running', started_at=datetime.now(timezone.utc))
    repository.create_run(run)
    restored = repository.get_run(context.run_id).context
    assert restored == context
    assert restored.experiment_id == 'exp-1' and restored.sample_id == 'id0'
    bad = {**public, 'answer': 'leak'}
    write_json(tmp_path / 'bad.json', bad)
    with pytest.raises(ValueError):
        load_context(tmp_path / 'bad.json')


def test_contexts_do_not_share_documents(tmp_path):
    first = record()
    second = record(1)
    second['context'] = [['Second', ['Quantum physics laboratory.']]]
    base = resolve_system()
    a = build_batch_context(base, first, tmp_path / 'a.json', 'a')
    b = build_batch_context(base, second, tmp_path / 'b.json', 'b')
    assert retriever_from_snapshot(a.config_snapshot).search('Cedar')
    assert not retriever_from_snapshot(b.config_snapshot).search('Cedar')


def test_selection_is_fixed_independent_of_order_and_labels():
    records = [record(i, 'bridge' if i < 8 else 'comparison') for i in range(16)]
    expected = make_manifest(records, 'hash')
    for item in records:
        item['answer'] = 'different'
    assert make_manifest(list(reversed(records)), 'hash') == expected
    assert len(expected['samples']) == 10
    assert sum(item['type'] == 'bridge' for item in expected['samples']) == 5


@pytest.mark.parametrize('prediction,gold,expected', [
    ('The Northbridge!', 'northbridge', {'em': 1.0, 'f1': 1.0}),
    ('yes indeed', 'yes', {'em': 0.0, 'f1': 0.0}),
    ('', 'x', {'em': 0.0, 'f1': 0.0}),
    ('red red blue', 'red blue', {'em': 0.0, 'f1': 0.8}),
])
def test_answer_metric_semantics(prediction, gold, expected):
    assert answer_scores(prediction, gold) == expected


def test_evidence_recovery_rejects_ambiguous_or_false_coordinates():
    context = [['A', ['same', 'same']], ['B', ['unique']]]
    facts, recovered = evidence_facts([
        {'source_title': 'A', 'text': 'same'},
        {'source_title': 'B', 'text': 'unique'},
        {'source_title': 'B', 'text': 'unique', 'source_paragraph': 8},
    ], context)
    assert facts == recovered == [['B', 0]]
    assert supporting_scores([['B', 0], ['B', 0]], [['B', 0]]) == {'sp_em': 1.0, 'sp_f1': 1.0}


def test_batch_keeps_failed_samples_and_continues(tmp_path):
    records = [record(i, 'bridge' if i < 5 else 'comparison') for i in range(10)]
    dataset, manifest = tmp_path / 'dataset.json', tmp_path / 'manifest.json'
    write_json(dataset, records)
    write_json(manifest, make_manifest(records, sha256_file(dataset)))
    from datetime import datetime, timezone
    from src.runtime.models import RunError

    class StubService:
        def __init__(self):
            self.calls = 0

        def run(self, context):
            self.calls += 1
            now = datetime.now(timezone.utc)
            failed = self.calls == 1
            return RunDetail(run=RunRecord(context=context, status='failed' if failed else 'completed',
                started_at=now, finished_at=now,
                error=RunError(error_type='test', message='controlled') if failed else None,
                final_state={'answer': 'GOLD_ONLY_LABEL', 'verified': not failed}, metrics=RunMetrics()))

    service = StubService()
    summary = run_batch(dataset, manifest, tmp_path / 'experiment', service=service)
    assert service.calls == 10 and summary['sample_count'] == 10
    assert summary['completed'] == 9 and summary['em'] == 0.9
    assert len((tmp_path / 'experiment' / 'results.jsonl').read_text(encoding='utf-8').splitlines()) == 10
    with pytest.raises(FileExistsError):
        run_batch(dataset, manifest, tmp_path / 'experiment', service=service)
