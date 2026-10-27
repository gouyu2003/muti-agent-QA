"""Platform batch evaluation endpoints. Listing/creation never invokes a model."""
import json
import sqlite3

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from src.evaluation.experiment_config import METHODS
from src.evaluation.platform_store import EvaluationStore, JobRequest, EvaluationFailure
from src.evaluation.platform_worker import exported_csv, reconcile, start_job, stop_job
from src.runtime.sqlite_repository import SQLiteRunRepository

router = APIRouter(prefix='/api/v1/evaluations')


def store():
    return EvaluationStore()


class StartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    allow_model_calls: bool = Field(default=False, strict=True)
    retry_failed: bool = Field(default=False, strict=True)


@router.get('/question-banks')
def banks():
    return {'items': store().banks()}


@router.post('/question-banks', status_code=201)
async def import_bank(name: str = Form(...), description: str = Form(''), bank_id: str | None = Form(None), file: UploadFile = File(...)):
    raw = await file.read(100 * 1024 * 1024 + 1)
    if len(raw) > 100 * 1024 * 1024:
        raise EvaluationFailure('JSON 文件不能超过 100 MiB。', 413)
    try:
        value = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvaluationFailure('请上传 UTF-8 编码的 JSON 数组。') from exc
    return store().import_bank(name, description, value, bank_id)


@router.get('/options')
def options(request: Request):
    systems = []
    for item in request.app.state.system_store.list():
        for version in item['versions']:
            snapshot = version['snapshot']
            if snapshot.get('schema_version') == '3' and snapshot.get('protocol_document', {}).get('spec', {}).get('language_version') == '2':
                systems.append(dict(system_id=item['system_id'], version=version['version'], name=version['name']))
    return dict(methods=METHODS, systems=systems)


@router.get('/jobs')
def jobs():
    catalog = store()
    items = catalog.jobs()
    for item in items:
        reconcile(catalog, item['job_id'])
    return {'items': catalog.jobs()}


@router.post('/jobs', status_code=201)
def create_job(body: JobRequest, request: Request):
    return store().create_job(body, request.app.state.system_store)


@router.get('/jobs/{job_id}')
def job(job_id: str):
    catalog = store()
    reconcile(catalog, job_id)
    return catalog.job(job_id)


@router.post('/jobs/{job_id}/start')
def start(job_id: str, body: StartRequest):
    return start_job(store(), job_id, allow_model_calls=body.allow_model_calls, retry_failed=body.retry_failed)


@router.post('/jobs/{job_id}/stop')
def stop(job_id: str):
    return stop_job(store(), job_id)


def result_rows(catalog, job_id, offset=0, limit=None):
    path = catalog.job_directory(job_id) / 'tasks.sqlite3'
    if not path.exists():
        return dict(items=[], total=0)
    with sqlite3.connect(path) as db:
        try:
            total = db.execute('SELECT COUNT(*) FROM attempts WHERE result_json IS NOT NULL').fetchone()[0]
            query = ('SELECT result_json,attempt,t.repeat FROM attempts a JOIN tasks t USING(task_id) '
                     'WHERE result_json IS NOT NULL ORDER BY task_id,attempt')
            rows = db.execute(query + (' LIMIT ? OFFSET ?' if limit is not None else ''),
                              (limit, offset) if limit is not None else ()).fetchall()
        except sqlite3.OperationalError:
            rows, total = [], 0
    return dict(items=[{**json.loads(r[0]), 'attempt': r[1], 'repeat': r[2]} for r in rows], total=total)


@router.get('/jobs/{job_id}/results')
def results(job_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return dict(**result_rows(store(), job_id, offset, limit), offset=offset, limit=limit)


@router.get('/jobs/{job_id}/runs/{run_id}')
def run_detail(job_id: str, run_id: str):
    catalog = store()
    path = catalog.job_directory(job_id) / 'runs.sqlite3'
    if not path.exists():
        raise HTTPException(404, '运行记录不存在。')
    detail = SQLiteRunRepository(path).get_run_detail(run_id)
    if not detail or detail.run.context.experiment_id != job_id:
        raise HTTPException(404, '运行记录不存在。')
    return detail


@router.get('/jobs/{job_id}/export')
def export(job_id: str, format: str = Query('csv', pattern='^(csv|json)$')):
    catalog = store()
    rows = result_rows(catalog, job_id)['items']
    if format == 'csv':
        return Response(exported_csv(rows), media_type='text/csv', headers={'Content-Disposition': f'attachment; filename="{job_id}.csv"'})
    return Response(json.dumps(dict(job=catalog.job(job_id), results=rows), ensure_ascii=False), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{job_id}.json"'})
