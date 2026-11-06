"""Claim ownership and heartbeat before importing expensive runtime dependencies."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from threading import Event, Thread
from time import monotonic

from src.process_lock import process_lock


def bootstrap(database, run_id, token, *, loader=None):
    database = Path(database).resolve()
    root = database.parent / (database.stem + '-interactive') / run_id
    with process_lock(root):
        stop = Event()
        start = monotonic()
        def heartbeat():
            while not stop.is_set():
                with closing(sqlite3.connect(database, timeout=5)) as db, db:
                    row = db.execute('SELECT i.token,i.cancel_at,r.status FROM interactive_runs i JOIN runs r USING(run_id) WHERE i.run_id=?', (run_id,)).fetchone()
                    if not row or row[0] != token or row[1] or row[2] != 'running' or monotonic()-start > 120:
                        os._exit(0)
                    db.execute('UPDATE interactive_runs SET heartbeat_at=? WHERE run_id=? AND token=?',
                               (datetime.now(timezone.utc).isoformat(), run_id, token))
                stop.wait(2)
        def guarded_heartbeat():
            try:
                heartbeat()
            except (sqlite3.Error, OSError):
                os._exit(1)
        watcher = Thread(target=guarded_heartbeat, daemon=True)
        watcher.start()
        try:
            if loader is None:
                from src.runtime.interactive_worker import execute
            else:
                execute = loader()
        finally:
            stop.set()
            watcher.join(timeout=6)
        # Ownership stays held across the handover; there is no unlocked startup gap.
        execute(database, run_id, token, lock_owned=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    bootstrap(args.database, args.run, args.token)


if __name__ == '__main__':
    main()
