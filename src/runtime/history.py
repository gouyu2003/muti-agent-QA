"""Read daily runs and explicitly registered, immutable experiment archives.

RunService continues to write only to its own repository. Archive discovery never
recurses into trial/checkpoint directories and never initializes a database.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import sqlite3

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.runtime.models import RunDetail, RunRecord, RunStatsSummary
from src.runtime.repository import RepositoryError, RunRepository
from src.runtime.sqlite_repository import SQLiteRunRepository

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / 'system-design' / 'experiments' / 'history.json'


class ExperimentOption(BaseModel):
    experiment_id: str
    name: str


class ArchiveSpec(ExperimentOption):
    model_config = ConfigDict(extra='forbid')
    database: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class ReadOnlyArchive(SQLiteRunRepository):
    def initialize(self) -> None:
        raise RepositoryError('实验档案仅支持读取')

    def _open_connection(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise RepositoryError('实验档案不存在')
        wal = Path(str(self.db_path) + '-wal')
        if wal.exists() and wal.stat().st_size:
            raise RepositoryError('实验仍有未归档的写入，请完成数据库检查点后登记')
        # Only finalized archives are registered. immutable prevents journal/SHM
        # creation, and mode=ro makes an accidental write fail at the SQLite layer.
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only = ON')
        return connection


class HistoryReader:
    def __init__(self, primary: RunRepository):
        self.primary = primary
        self.archives: list[tuple[ArchiveSpec, ReadOnlyArchive]] = []
        catalog = Path(os.getenv('MAQA_EXPERIMENT_CATALOG_PATH') or DEFAULT_CATALOG)
        root = Path(os.getenv('MAQA_EXPERIMENTS_DIR') or ROOT / 'data' / 'experiments').resolve()
        if not catalog.is_file():
            return
        try:
            raw = json.loads(catalog.read_text(encoding='utf-8'))
            specs = [ArchiveSpec.model_validate(item) for item in raw['experiments']]
            ids, paths = set(), set()
            for spec in specs:
                relative = Path(spec.database)
                path = (root / relative).resolve()
                if relative.is_absolute() or not path.is_relative_to(root):
                    raise ValueError('Archive path must stay inside the experiments directory')
                if spec.experiment_id in ids or path in paths:
                    raise ValueError('Duplicate experiment registration')
                ids.add(spec.experiment_id)
                paths.add(path)
                # A fresh checkout has no local experiment data. Daily history
                # remains available; absent archives are not offered as choices.
                if path.is_file():
                    self.archives.append((spec, ReadOnlyArchive(path)))
        except (OSError, ValueError, TypeError, KeyError, ValidationError) as exc:
            raise RepositoryError('实验档案配置不可用') from exc

    def experiments(self) -> list[ExperimentOption]:
        return [ExperimentOption(experiment_id=spec.experiment_id, name=spec.name)
                for spec, _ in self.archives]

    @staticmethod
    def _read(repository, filters):
        offset = 0
        while True:
            page = repository.list_runs(limit=100, offset=offset, **filters)
            yield from page
            if len(page) < 100:
                break
            offset += len(page)

    def records(self, *, experiment_id=None, system_id=None, **filters) -> list[RunRecord]:
        found: dict[str, RunRecord] = {}

        def add(record):
            if system_id is not None and record.context.config_snapshot.get('system_ref', {}).get('system_id') != system_id:
                return
            if experiment_id is not None and record.context.experiment_id != experiment_id:
                return
            if record.context.run_id in found:
                raise RepositoryError('不同档案存在重复的运行 ID')
            found[record.context.run_id] = record

        for record in self._read(self.primary, filters):
            add(record)
        if filters.get('entrypoint') in (None, 'batch'):
            for spec, repository in self.archives:
                if experiment_id is not None and spec.experiment_id != experiment_id:
                    continue
                for record in self._read(repository, {**filters, 'entrypoint': 'batch'}):
                    if record.context.experiment_id != spec.experiment_id:
                        raise RepositoryError('实验档案与登记信息不一致')
                    add(record)
        return sorted(found.values(), key=lambda run: (run.started_at, run.context.run_id), reverse=True)

    def detail(self, run_id: str, experiment_id=None) -> RunDetail | None:
        matches = []
        primary = self.primary.get_run_detail(run_id)
        if primary and (experiment_id is None or primary.run.context.experiment_id == experiment_id):
            matches.append(primary)
        for spec, repository in self.archives:
            if experiment_id is not None and spec.experiment_id != experiment_id:
                continue
            detail = repository.get_run_detail(run_id)
            if detail:
                if detail.run.context.entrypoint != 'batch' or detail.run.context.experiment_id != spec.experiment_id:
                    raise RepositoryError('实验档案与登记信息不一致')
                matches.append(detail)
        if len(matches) > 1:
            raise RepositoryError('不同档案存在重复的运行 ID')
        return matches[0] if matches else None

    def export(self, **filters):
        # Assemble before streaming, so archive errors produce an HTTP error
        # rather than a partially successful download.
        return iter([self.detail(run.context.run_id, run.context.experiment_id).model_dump_json() + '\n'
                     for run in self.records(**filters)])


def summarize_history(records: list[RunRecord]) -> RunStatsSummary:
    statuses = Counter(run.status for run in records)
    verified = sum(run.status == 'completed' and bool((run.final_state or {}).get('verified')) for run in records)
    durations = [run.metrics.duration_ms for run in records if run.metrics.duration_ms is not None]
    retries = [run.metrics.plan_retry_count + run.metrics.retrieval_retry_count
               for run in records if run.status != 'running']
    failures = Counter()
    for run in records:
        kind = run.error.error_type if run.error else (run.final_state or {}).get('failure_type')
        if kind and kind != 'none':
            failures[kind] += 1
    return RunStatsSummary(total_runs=len(records), completed_runs=statuses['completed'],
        failed_runs=statuses['failed'], cancelled_runs=statuses['cancelled'], running_runs=statuses['running'],
        verified_runs=verified, completion_rate=statuses['completed'] / len(records) if records else 0.0,
        verification_pass_rate=verified / statuses['completed'] if statuses['completed'] else 0.0,
        average_duration_ms=sum(durations) / len(durations) if durations else None,
        average_retry_count=sum(retries) / len(retries) if retries else None,
        failure_types=dict(sorted(failures.items())))
