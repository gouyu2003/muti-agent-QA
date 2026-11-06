"""Detached interactive QA worker; the CLI accepts only an already reserved run."""
from __future__ import annotations

import argparse
import logging
import os
from threading import Event, Thread
from time import monotonic
from contextlib import nullcontext
from pathlib import Path

from dotenv import load_dotenv

from src.evaluation.experiment_store import experiment_lock
from src.runtime.interactive import InteractiveRuns, ROOT, seconds_since
from src.observability import utcnow, read_observations, activity_snapshot
from src.runtime.service import RunService
from src.runtime.repository import InvalidRunTransitionError
from src.runtime.sqlite_repository import SQLiteRunRepository


def execute(repository, run_id, token, *, service=None, hard_exit=os._exit, lock_owned=False):
    if isinstance(repository, (str, Path)):
        load_dotenv(ROOT / '.env')
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
        logging.getLogger('httpx').setLevel(logging.WARNING)
        repository = SQLiteRunRepository(repository)
    manager = InteractiveRuns(repository)
    with nullcontext() if lock_owned else experiment_lock(manager.root / run_id):
        row = manager.row(run_id)
        run = repository.get_run(run_id)
        if row['token'] != token or run is None or run.status != 'running':
            return
        cancelled, stopped = Event(), Event()
        start = monotonic()
        limits = run.context.config_snapshot.get('protocol_document', {}).get('spec', {}).get('limits', {})
        total_limit, node_limit = limits.get('timeout_seconds', 600), limits.get('node_timeout_seconds', 120)

        def control():
            while not stopped.is_set():
                row = manager.row(run_id)
                with repository._connection() as db:
                    db.execute("UPDATE interactive_runs SET heartbeat_at=?,phase=CASE WHEN cancel_at IS NULL THEN 'running' ELSE 'cancelling' END WHERE run_id=?", (utcnow(), run_id))
                active = activity_snapshot(read_observations(repository.db_path, run_id))
                expired = monotonic()-start > total_limit or any(
                    r.get('name') == 'model_request' and seconds_since(r['timestamp']) > node_limit for r in active)
                if row['cancel_at'] or expired:
                    cancelled.reason = 'timeout' if expired else 'cancelled'
                    cancelled.set()
                    deadline = monotonic()+5
                    while not stopped.wait(.1) and monotonic() < deadline:
                        pass
                    if not stopped.is_set():
                        try:
                            manager.settle(run_id, 'failed' if expired else 'cancelled',
                                           'timeout' if expired else 'cancelled',
                                           '运行达到时间上限，已结束本地执行。' if expired else '已停止本地执行；远端未返回请求的结果及用量未知。')
                        finally:
                            hard_exit(0)
                    return
                stopped.wait(2)

        watcher = Thread(target=control, daemon=True)
        watcher.start()
        try:
            (service or RunService(repository)).run(run.context, prepared=True, cancellation=cancelled)
        except InvalidRunTransitionError:
            saved = repository.get_run(run_id)
            if saved is None or saved.status == 'running':
                raise
            # A watchdog may have durably settled while an uncooperative request was returning.
            logging.getLogger(__name__).info('run=%s terminal=%s already persisted', run_id, saved.status)
        finally:
            stopped.set()
            watcher.join(timeout=3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    load_dotenv(ROOT / '.env')
    repository = SQLiteRunRepository(args.database)
    # The API initializes/migrates before reserving the run. A worker never migrates a live store.
    execute(repository, args.run, args.token)


if __name__ == '__main__':
    main()
