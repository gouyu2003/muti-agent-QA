"""Single-writer OS lock and append-only attempt ledger (no remote exactly-once claim)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


def utcnow():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def experiment_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / '.execution.lock').open('a+b')
    acquired = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise ValueError('Experiment is locked by another process; do not delete its lock file') from exc
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class Ledger:
    def __init__(self, directory, *, existing=False):
        path = Path(directory) / 'tasks.sqlite3'
        if existing and not path.is_file():
            raise ValueError('Task ledger is missing; refusing to reconstruct or replay tasks')
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
                task_id INTEGER PRIMARY KEY, sample_id TEXT NOT NULL, method_id TEXT NOT NULL,
                repeat INTEGER NOT NULL, UNIQUE(sample_id, method_id, repeat));
            CREATE TABLE IF NOT EXISTS attempts (
                task_id INTEGER NOT NULL, attempt INTEGER NOT NULL, run_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                result_json TEXT, PRIMARY KEY(task_id, attempt));
            CREATE TABLE IF NOT EXISTS sessions (started_at TEXT, finished_at TEXT, wall_seconds REAL);
        ''')

    def close(self):
        self.db.close()

    @staticmethod
    def planned(records, config):
        for repeat in range(config.repeats):
            for i, record in enumerate(records):
                methods = config.methods
                if config.rotate_methods:
                    offset = (i + repeat) % len(methods)
                    methods = methods[offset:] + methods[:offset]
                for method in methods:
                    yield record['_id'], method, repeat

    def initialize(self, records, config):
        with self.db:
            self.db.executemany('INSERT INTO tasks(sample_id,method_id,repeat) VALUES (?,?,?)',
                                self.planned(records, config))

    def validate(self, records, config):
        expected = [(i + 1, *row) for i, row in enumerate(self.planned(records, config))]
        actual = [tuple(r.values()) for r in self.tasks()]
        if actual != expected:
            raise ValueError('Task ledger differs from frozen experiment manifest')
        orphan = self.db.execute('SELECT 1 FROM attempts a LEFT JOIN tasks t USING(task_id) WHERE t.task_id IS NULL').fetchone()
        if orphan:
            raise ValueError('Task ledger contains orphan attempts')
        for task in self.tasks():
            attempts = self.attempts(task['task_id'])
            if [a['attempt'] for a in attempts] != list(range(1, len(attempts) + 1)):
                raise ValueError('Attempt ledger sequence is incomplete')

    def tasks(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM tasks ORDER BY task_id')]

    def attempts(self, task_id=None):
        query = 'SELECT a.*, t.sample_id, t.method_id, t.repeat FROM attempts a JOIN tasks t USING(task_id)'
        params = ()
        if task_id is not None:
            query += ' WHERE task_id=?'; params = (task_id,)
        return [dict(r) for r in self.db.execute(query + ' ORDER BY task_id,attempt', params)]

    def start(self, task, run_id):
        number = len(self.attempts(task['task_id'])) + 1
        with self.db:
            self.db.execute('INSERT INTO attempts(task_id,attempt,run_id,status,started_at) VALUES (?,?,?,?,?)',
                            (task['task_id'], number, run_id, 'running', utcnow()))
        return number

    def finish(self, run_id, status, result):
        with self.db:
            self.db.execute('UPDATE attempts SET status=?, finished_at=?, result_json=? WHERE run_id=?',
                            (status, utcnow(), json.dumps(result, ensure_ascii=False), run_id))

    def session_start(self):
        with self.db:
            return self.db.execute('INSERT INTO sessions(started_at) VALUES (?)', (utcnow(),)).lastrowid

    def session_finish(self, row_id, seconds):
        with self.db:
            self.db.execute('UPDATE sessions SET finished_at=?,wall_seconds=? WHERE rowid=?', (utcnow(), seconds, row_id))
