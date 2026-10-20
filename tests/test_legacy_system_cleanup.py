"""Guarded one-off deletion, recovery proof and preservation, in isolated storage."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import pytest
import scripts.cleanup_legacy_systems as task
from src.runtime.system_store import SystemStore, fingerprint
from src.runtime.sqlite_repository import SQLiteRunRepository
from test_sqlite_repository import make_running, make_completed, make_event


@pytest.fixture
def catalogs(tmp_path):
    store = SystemStore(tmp_path / 'data/systems')
    with store.connection() as db:
        for key, (revision, versions) in {**task.TARGETS, 'keep_me': (1, [(1, 1)])}.items():
            snapshot = dict(system_id=key, system_version='draft', agents={}, protocol={})
            snapshot['config_sha256'] = fingerprint(snapshot)
            doc = json.dumps(dict(name=key, snapshot=snapshot))
            db.execute('INSERT INTO systems VALUES (?,?,?,?,?)', (key, revision, doc, 'now', 'now'))
            for version, rev in versions:
                db.execute('INSERT INTO releases VALUES (?,?,?,?,?)', (key, version, rev, doc, 'now'))
    run_path = tmp_path / 'data/runtime/runs.sqlite3'
    repo = SQLiteRunRepository(run_path); repo.initialize()
    for run_id, (key, kind, revision, version) in task.EXPECTED_RUNS.items():
        record = make_running(run_id)
        record.context.config_snapshot = {'system_ref': dict(system_id=key, kind=kind, draft_revision=revision, system_version=version)}
        repo.create_run(record); repo.append_event(make_event(run_id, 0)); repo.update_run(make_completed(record))
    marker = tmp_path / 'data/knowledge/material.txt'; marker.parent.mkdir(); marker.write_text('Preserve all materials')
    return tmp_path, store.database, run_path


def state(path):
    with closing(task.connect(path)) as db:
        return task.database_state(db)


def test_only_exact_targets_are_deleted_after_recoverable_backup(catalogs):
    root, system, runs = catalogs
    before, history = state(system), state(runs)
    dry = task.cleanup(root)
    assert not dry['applied'] and state(system) == before
    result = task.cleanup(root, apply=True)
    assert result['deleted_systems'] == 2 and result['deleted_releases'] == 3
    assert result['history_preserved'] and result['restored_backup_verified']
    assert state(runs) == history
    assert [r[0] for r in state(system)['tables']['systems']] == ['keep_me']
    assert state(Path(result['backup'])) == before
    assert task.verify_backup(Path(result['backup']), before) == result['backup_sha256']
    assert task.history_proof(runs) == result['history_sha256']


@pytest.mark.parametrize('change', ['revision', 'versions', 'reference', 'running', 'fingerprint', 'cross_reference'])
def test_changed_targets_or_references_stop_cleanup(catalogs, change):
    root, system, runs = catalogs
    key = next(iter(task.TARGETS))
    with sqlite3.connect(system) as db:
        if change == 'revision': db.execute('UPDATE systems SET revision=99 WHERE system_id=?', (key,))
        if change == 'versions': db.execute('DELETE FROM releases WHERE system_id=? AND version=2', (key,))
        if change == 'fingerprint':
            doc = json.loads(db.execute('SELECT draft FROM systems WHERE system_id=?', (key,)).fetchone()[0])
            doc['snapshot']['config_sha256'] = 'invalid'
            db.execute('UPDATE systems SET draft=? WHERE system_id=?', (json.dumps(doc), key))
        if change == 'cross_reference':
            doc = json.loads(db.execute("SELECT draft FROM systems WHERE system_id='keep_me'").fetchone()[0])
            doc['parent'] = key
            db.execute("UPDATE systems SET draft=? WHERE system_id='keep_me'", (json.dumps(doc),))
    with sqlite3.connect(runs) as db:
        if change == 'reference': db.execute('DELETE FROM trace_events'); db.execute('DELETE FROM runs')
        if change == 'running': db.execute("UPDATE runs SET status='running',finished_at=NULL")
    before = state(system)
    with pytest.raises(ValueError, match='Cleanup stopped'):
        task.cleanup(root, apply=True)
    assert state(system) == before
    assert not list(system.parent.glob('systems.pre-unification*'))


def test_backup_verification_failure_never_deletes(catalogs, monkeypatch):
    root, system, _ = catalogs
    before = state(system)
    def reject(*args): raise ValueError('Backup recovery failed')
    monkeypatch.setattr(task, 'verify_backup', reject)
    with pytest.raises(ValueError, match='recovery failed'): task.cleanup(root, apply=True)
    assert state(system) == before


def test_post_delete_validation_failure_rolls_back(catalogs, monkeypatch):
    root, system, _ = catalogs
    before = state(system)
    proof = task.history_proof
    calls = 0
    def changed(path):
        nonlocal calls
        calls += 1
        return proof(path) if calls == 1 else 'changed'
    monkeypatch.setattr(task, 'history_proof', changed)
    with pytest.raises(ValueError, match='History changed'): task.cleanup(root, apply=True)
    assert state(system) == before
