"""Archive and remove evaluation records without changing the benchmark runners."""
from contextlib import contextmanager
from pathlib import Path
import re
from shutil import rmtree

from src.evaluation import gsm8k_platform as gsm8k, mmlu_pro
from src.evaluation.experiment_store import experiment_lock
from src.evaluation.platform_store import EvaluationFailure


def _job_path(root, job_id):
    path = root / job_id
    if path.parent != root or path.name != job_id or path.resolve() != path:
        raise EvaluationFailure('实验目录超出允许的存储范围。', 409)
    return path


def _remove_files(directory: Path):
    try:
        rmtree(directory)
    except FileNotFoundError:
        if directory.exists():
            raise EvaluationFailure('实验文件尚未清理完成，请重试。', 409) from None
    except OSError as exc:
        raise EvaluationFailure('无法清理实验文件，请关闭占用文件的程序后重试。', 409) from exc


def delete_job(catalog, job_id):
    directory = _job_path(catalog.root / 'jobs', job_id)
    with catalog.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise EvaluationFailure('评测任务不存在。', 404)
        if row['status'] in ('queued', 'running', 'stopping'):
            raise EvaluationFailure('请等实验暂停或结束后再删除。', 409)
        if directory.exists():
            try:
                with experiment_lock(directory):
                    pass
            except ValueError as exc:
                raise EvaluationFailure('后台仍在写入实验结果，请稍后重试。', 409) from exc
            _remove_files(directory)
        db.execute('DELETE FROM job_catalog WHERE job_id=?', (job_id,))
        db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
    return dict(job_id=job_id, deleted=True)


def job_archived(catalog, job_id):
    with catalog.connection() as db:
        return db.execute('SELECT 1 FROM job_catalog WHERE job_id=? AND archived=1',
                          (job_id,)).fetchone() is not None


def benchmark_detail(catalog, item):
    return dict(item, archived=job_archived(catalog, item['job_id']))


def benchmark_list(catalog, items, include_archived):
    with catalog.connection() as db:
        archived = {row[0] for row in db.execute('SELECT job_id FROM job_catalog WHERE archived=1')}
    return [dict(item, archived=item['job_id'] in archived) for item in items
            if include_archived or item['job_id'] not in archived]


def _gsm_cleanup_directory(job_id):
    if not re.fullmatch(r'gsm_[0-9a-f]{32}', job_id):
        raise EvaluationFailure('复现实验不存在。', 404)
    directory = _job_path(gsm8k.jobs_root(), job_id)
    if not directory.is_dir():
        raise EvaluationFailure('复现实验不存在。', 404)
    return directory


@contextmanager
def _benchmark_lock(catalog, engine, job_id, *, deleting=False):
    if engine is gsm8k:
        if deleting:
            _gsm_cleanup_directory(job_id)
        else:
            gsm8k.job_dir(job_id)
    else:
        root = mmlu_pro.jobs_root()
        mmlu_pro._lock_path(root, job_id)
        with mmlu_pro.connection(root) as db:
            mmlu_pro._job(db, job_id)
    # Keep this lock outside the run directory so Windows can remove that directory.
    with gsm8k._lock(catalog.root / '.job-actions' / f'{job_id}.lock'):
        yield


def start_benchmark_job(catalog, engine, job_id, allow_model_calls, **options):
    with _benchmark_lock(catalog, engine, job_id):
        if job_archived(catalog, job_id):
            raise EvaluationFailure('实验已归档，请先取消归档再启动。', 409)
        return dict(engine.start_job(job_id, allow_model_calls, **options), archived=False)


def stop_benchmark_job(catalog, engine, job_id):
    with _benchmark_lock(catalog, engine, job_id):
        return benchmark_detail(catalog, engine.stop_job(job_id))


def change_benchmark_job(catalog, engine, job_id, *, archived):
    with _benchmark_lock(catalog, engine, job_id, deleting=archived is None):
        if engine is gsm8k:
            directory = _gsm_cleanup_directory(job_id) if archived is None else gsm8k.job_dir(job_id)
            with gsm8k._lock(directory / 'supervisor.lock'):
                if gsm8k._core_busy(directory):
                    raise EvaluationFailure('请等实验暂停或结束后再操作。', 409)
            if archived is None:
                _remove_files(directory)
        else:
            root = mmlu_pro.jobs_root()
            # Use the existing recovery path if a worker exited without a terminal state.
            with mmlu_pro.connection(root) as db:
                row, _ = mmlu_pro._job(db, job_id)
            if row['state'] in ('running', 'stopping'):
                mmlu_pro.view_job(job_id)
            with mmlu_pro._lock(mmlu_pro._lock_path(root, job_id)), mmlu_pro.connection(root) as db:
                db.execute('BEGIN IMMEDIATE')
                row, _ = mmlu_pro._job(db, job_id)
                if row['state'] in ('running', 'stopping'):
                    raise EvaluationFailure('请等实验暂停或结束后再操作。', 409)
                if archived is None:
                    db.execute('DELETE FROM pending_resumes WHERE job_id=?', (job_id,))
                    db.execute('DELETE FROM superseded_calls WHERE job_id=?', (job_id,))
                    db.execute('DELETE FROM calls WHERE job_id=?', (job_id,))
                    db.execute('DELETE FROM tasks WHERE job_id=?', (job_id,))
                    db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
        with catalog.connection() as db:
            if archived:
                db.execute('INSERT INTO job_catalog (job_id,archived) VALUES (?,1) '
                           'ON CONFLICT(job_id) DO UPDATE SET archived=1', (job_id,))
            else:
                db.execute('DELETE FROM job_catalog WHERE job_id=?', (job_id,))
    return dict(job_id=job_id, deleted=True) if archived is None else dict(job_id=job_id, archived=archived)
