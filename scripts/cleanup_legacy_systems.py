"""One-off cleanup of the two approved stage 4.1 systems. Dry-run by default.

python -m scripts.cleanup_legacy_systems --apply
No configurable IDs, general deletion endpoint, migration or history deletion.
"""
from argparse import ArgumentParser
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from src.runtime.history import ReadOnlyArchive
from src.runtime.system_store import fingerprint

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    'sys_f45fbc0a3aef4338bc523bc4adec5978': (2, [(1, 1), (2, 2)]),
    'sys_d14bd0cce54344c58d7f721ef20a719d': (1, [(1, 1)]),
}
EXPECTED_RUNS = {
    'a8f4566c-ef19-44cd-aae9-894249458f69': (next(iter(TARGETS)), 'draft', 1, None),
    '662705d2-9db4-47cf-877d-5ef714a37f63': (list(TARGETS)[1], 'published', 1, 'v1'),
}


def require(condition, message):
    if not condition:
        raise ValueError(message + ' Cleanup stopped; no broader deletion is permitted.')


def connect(path, mode='ro'):
    return sqlite3.connect(path.resolve().as_uri() + f'?mode={mode}', uri=True, timeout=5)


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def database_state(db):
    schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    tables = {}
    for kind, name, _, _ in schema:
        if kind == 'table':
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = sorted(db.execute(f'SELECT * FROM {quoted}').fetchall(), key=repr)
    return dict(schema=schema, tables=tables)


def inspect_systems(db):
    require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'Systems database integrity failed.')
    require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'Foreign key validation failed.')
    systems = {r[0]: r for r in db.execute('SELECT * FROM systems')}
    for key, (revision, releases) in TARGETS.items():
        require(key in systems and systems[key][1] == revision, 'Target ID or draft revision changed.')
        actual = db.execute('SELECT version,revision FROM releases WHERE system_id=? ORDER BY version', (key,)).fetchall()
        require(actual == releases, 'Target published versions changed.')
        docs = [json.loads(systems[key][2])] + [json.loads(r[0]) for r in db.execute('SELECT document FROM releases WHERE system_id=?', (key,))]
        for doc in docs:
            snapshot = doc['snapshot']
            require(snapshot.get('schema_version', '1') == '1', 'Target is no longer a legacy schema 1 system.')
            require(snapshot.get('config_sha256') == fingerprint(snapshot), 'Frozen system fingerprint failed.')
    # A new reference from another saved system is outside the approved cleanup.
    for table, column in [('systems', 'draft'), ('releases', 'document')]:
        for key, raw in db.execute(f'SELECT system_id,{column} FROM {table}'):
            if key not in TARGETS:
                require(not any(target in raw for target in TARGETS), 'Unexpected cross-system reference found.')
    return dict(systems=len(systems), releases=db.execute('SELECT count(*) FROM releases').fetchone()[0])


def inspect_references(db):
    refs = {}
    for run_id, status, raw in db.execute('SELECT run_id,status,config_snapshot_json FROM runs'):
        ref = json.loads(raw).get('system_ref', {})
        if ref.get('system_id') in TARGETS:
            require(status != 'running', 'Target has an in-flight run.')
            refs[run_id] = (ref['system_id'], ref.get('kind'), ref.get('draft_revision'), ref.get('system_version'))
    require(refs == EXPECTED_RUNS, 'Target run reference set changed.')
    return refs


def history_proof(path):
    reader = ReadOnlyArchive(path)
    details = {}
    for key in EXPECTED_RUNS:
        detail = reader.get_run_detail(key)
        require(detail is not None and detail.run.status != 'running', 'History detail is unavailable.')
        details[key] = detail.model_dump(mode='json')
    exported = {line['run']['context']['run_id']: line for line in map(json.loads, reader.export_runs_jsonl())}
    require(all(exported.get(key) == doc for key, doc in details.items()), 'History JSONL export did not match details.')
    return checksum(details)


def verify_backup(path, expected):
    # Restore the entire on-disk backup into a fresh SQLite database, then compare
    # every table, row and schema definition; a copied file hash alone is insufficient.
    with closing(connect(path)) as source, closing(sqlite3.connect(':memory:')) as restored:
        source.backup(restored)
        require(restored.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'Restored backup integrity failed.')
        require(database_state(restored) == expected, 'Restored backup contents differ.')
        inspect_systems(restored)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cleanup(root=ROOT, *, apply=False):
    root = Path(root).resolve()
    system_path = root / 'data/systems/systems.sqlite3'
    run_path = root / 'data/runtime/runs.sqlite3'
    require(system_path.is_file() and run_path.is_file(), 'Expected default databases are absent.')
    with closing(connect(system_path)) as db, closing(connect(run_path)) as runs:
        counts = inspect_systems(db)
        refs = inspect_references(runs)
    report = dict(applied=False, targets={k: dict(revision=v[0], releases=v[1]) for k, v in TARGETS.items()}, before=counts,
                  associated_runs=list(refs), history_sha256=history_proof(run_path))
    if not apply:
        return report

    # SQLite shared-memory/journal files are lock coordination, not independent
    # datasets, and Windows may prohibit reading them while the reservation is held.
    # The complete logical runtime database is separately compared inside the lock.
    protected = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'data').rglob('*')
                 if p.is_file() and p != system_path and not p.name.endswith(('-shm', '-wal', '-journal'))}
    # Reserve the run writer to prevent a new target run between reference checks
    # and deletion. No runtime row is written. Any lock timeout aborts the cleanup.
    with closing(connect(run_path, 'rw')) as runs:
        runs.execute('BEGIN IMMEDIATE')
        inspect_references(runs)
        run_state = database_state(runs)
        with closing(connect(system_path)) as source:
            source.execute('BEGIN')
            inspect_systems(source)
            before = database_state(source)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = system_path.with_name(f'systems.pre-unification-{stamp}.sqlite3')
            with backup.open('xb'):
                pass
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
        backup_sha = verify_backup(backup, before)
        expected = dict(schema=before['schema'], tables={**before['tables'],
            'systems': [r for r in before['tables']['systems'] if r[0] not in TARGETS],
            'releases': [r for r in before['tables']['releases'] if r[0] not in TARGETS]})
        with closing(connect(system_path, 'rw')) as db:
            db.execute('PRAGMA foreign_keys=ON')
            try:
                db.execute('BEGIN IMMEDIATE')
                require(database_state(db) == before, 'Systems changed after backup.')
                inspect_systems(db); inspect_references(runs)
                for key, (_, versions) in TARGETS.items():
                    require(db.execute('DELETE FROM releases WHERE system_id=?', (key,)).rowcount == len(versions), 'Release deletion count changed.')
                    require(db.execute('DELETE FROM systems WHERE system_id=?', (key,)).rowcount == 1, 'System deletion count changed.')
                require(database_state(db) == expected, 'Unrelated system data changed.')
                require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'Post-delete foreign keys failed.')
                require(database_state(runs) == run_state and history_proof(run_path) == report['history_sha256'], 'History changed.')
                require(all(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == sha for p, sha in protected.items()), 'Protected local data changed.')
                db.commit()
            except BaseException:
                db.rollback()
                raise
        runs.rollback()
    report.update(applied=True, backup=str(backup), backup_sha256=backup_sha, restored_backup_verified=True,
                  deleted_systems=2, deleted_releases=3, history_preserved=True, protected_files_verified=len(protected),
                  after=dict(systems=counts['systems'] - 2, releases=counts['releases'] - 3))
    backup.with_suffix('.verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(cleanup(apply=args.apply), ensure_ascii=False, indent=2))
