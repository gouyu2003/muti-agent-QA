"""Platform batch evaluation endpoints. Listing/creation never invokes a model."""
import json
import sqlite3
import hashlib
import io
import zipfile

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from src.evaluation.experiment_config import METHODS
from src.evaluation.platform_store import EvaluationStore, JobRequest, EvaluationFailure, published_snapshot
from src.evaluation.platform_worker import exported_csv, reconcile, start_job, stop_job
from src.runtime.sqlite_repository import SQLiteRunRepository
from src.evaluation.platform_datasets import (ManifestRequest, SplitRequest, ExposureRequest,
    import_manifest, create_splits, add_exposures, get_manifest, register)
from src.evaluation.platform_review import result_rows, compare_question, monitor
from src.evaluation.platform_workspace import PlanRequest, save_plan, get_plan, copy_job_plan, archive_job

router = APIRouter(prefix='/api/v1/evaluations')


def store():
    return EvaluationStore()


class StartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    allow_model_calls: bool = Field(default=False, strict=True)
    retry_failed: bool = Field(default=False, strict=True)


class ArchiveJobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    archived: bool = Field(strict=True)


class ImportCLIRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=120)


class GSMCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    methods: list[str] = Field(min_length=1, max_length=4)
    selection: str = Field(pattern='^(debug|calibration|custom|formal)$')
    count: int = Field(ge=1, le=100, strict=True)
    records: list[dict[str, str]] | None = None
    fake: bool = Field(default=False, strict=True)


@router.get('/gsm8k/jobs')
def gsm_jobs():
    from src.evaluation.gsm8k_platform import list_jobs
    return {'items': list_jobs()}


@router.post('/gsm8k/jobs', status_code=201)
def gsm_create(body: GSMCreateRequest):
    from src.evaluation.gsm8k_platform import create_job
    return create_job(body.name, body.methods, body.selection, body.count, body.records, body.fake)


@router.get('/gsm8k/jobs/{job_id}')
def gsm_job(job_id: str):
    from src.evaluation.gsm8k_platform import view_job
    return view_job(job_id, include_calls=True)


@router.post('/gsm8k/jobs/{job_id}/start')
def gsm_start(job_id: str, body: StartRequest):
    from src.evaluation.gsm8k_platform import start_job
    if body.retry_failed:
        raise EvaluationFailure('复现任务不自动重跑失败或状态不明的请求。', 409)
    return start_job(job_id, body.allow_model_calls)


@router.post('/gsm8k/jobs/{job_id}/stop')
def gsm_stop(job_id: str):
    from src.evaluation.gsm8k_platform import stop_job
    return stop_job(job_id)


@router.get('/plans')
def plans():
    catalog = store()
    with catalog.connection() as db:
        ids = [r[0] for r in db.execute('SELECT id FROM saved_plans ORDER BY created_at DESC')]
    return dict(items=[get_plan(catalog, key) for key in ids])


@router.post('/plans', status_code=201)
def create_plan(body: PlanRequest):
    return save_plan(store(), body)


@router.get('/plans/{plan_id}/export')
def export_plan(plan_id: str):
    return Response(json.dumps(get_plan(store(), plan_id), ensure_ascii=False), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{plan_id}.json"'})


@router.get('/archives/candidates')
def cli_candidates():
    from src.evaluation.platform_archives import candidates
    return dict(items=candidates())


@router.get('/archives')
def cli_archives():
    from src.evaluation.platform_archives import get_archive
    catalog = store()
    with catalog.connection() as db:
        keys = [r[0] for r in db.execute('SELECT id FROM cli_archives ORDER BY created_at DESC')]
    archives = [get_archive(catalog, key) for key in keys]
    return dict(items=[dict(archive_id=a['archive_id'], name=a['view']['name'], imported_at=a['imported_at'],
                           purpose=a['config']['purpose'], complete=a['original_report']['complete'],
                           fingerprint=a['fingerprint']) for a in archives])


@router.post('/archives', status_code=201)
def import_cli_archive(body: ImportCLIRequest):
    from src.evaluation.platform_archives import import_archive
    return import_archive(store(), body.key)


@router.get('/archives/{archive_id}')
def cli_archive(archive_id: str):
    from src.evaluation.platform_archives import get_archive
    return get_archive(store(), archive_id)


@router.get('/archives/{archive_id}/results')
def cli_results(archive_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                view: str = Query('all', pattern='^(first|latest|all)$'), status: str | None = None,
                method: str | None = None, label: str | None = None, search: str | None = Query(None, max_length=500)):
    from src.evaluation.platform_archives import archive_results
    return archive_results(store(), archive_id, offset, limit, view=view, status=status, method=method, label=label, search=search)


@router.get('/archives/{archive_id}/compare')
def cli_compare(archive_id: str, sample_id: str, repeat: int = Query(0, ge=0), view: str = Query('first', pattern='^(first|latest|all)$')):
    from src.evaluation.platform_archives import archive_results, get_archive
    catalog = store(); archive = get_archive(catalog, archive_id)
    if sample_id not in {s['sample_id'] for s in archive['manifest']['samples']} or repeat >= archive['config']['repeats']:
        raise HTTPException(404, '题目或重复轮次不存在。')
    rows = archive_results(catalog, archive_id, view=view, sample_id=sample_id, repeat=repeat)['items']
    question = rows[0].get('question', '') if rows else ''
    for method in archive['config']['methods']:
        if not any(r['method_id'] == method for r in rows):
            rows.append(dict(sample_id=sample_id, method_id=method, repeat=repeat, attempt=0, run_id=None,
                             status='pending', question=question, answer='', scores=None, labels=[], error=None))
    return dict(items=rows, question=question, gold=rows[0].get('gold') if rows else None)


@router.get('/archives/{archive_id}/runs/{run_id}')
def cli_run(archive_id: str, run_id: str):
    from src.evaluation.platform_archives import archive_detail
    return archive_detail(store(), archive_id, run_id)


@router.get('/archives/{archive_id}/export')
def cli_export(archive_id: str, format: str = Query('json', pattern='^(json|csv)$')):
    from src.evaluation.platform_archives import archive_results, get_archive
    catalog = store()
    archive = get_archive(catalog, archive_id)
    rows = archive_results(catalog, archive_id)['items']
    body = exported_csv(rows) if format == 'csv' else json.dumps(dict(archive=archive, results=rows), ensure_ascii=False)
    return Response(body, media_type='text/csv' if format == 'csv' else 'application/json',
                    headers={'Content-Disposition': f'attachment; filename="{archive_id}.{format}"'})


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
    return store().import_bank(name, description, value, bank_id, raw_sha256=hashlib.sha256(raw).hexdigest())


@router.post('/question-banks/{bank_id}/{version}/preview')
def preview_bank(bank_id: str, version: int):
    catalog = store()
    bank = catalog.bank(bank_id, version)
    rows = bank['records'][:3]
    with catalog.connection() as db:
        register(db, bank_id, [r['_id'] for r in rows], '用户主动预览题目')
    return dict(items=rows)


@router.get('/manifests')
def manifests():
    catalog = store()
    with catalog.connection() as db:
        ids = [r[0] for r in db.execute('SELECT id FROM manifests ORDER BY created_at DESC')]
    return dict(items=[get_manifest(catalog, key) for key in ids])


@router.post('/manifests', status_code=201)
def create_manifest(body: ManifestRequest):
    return import_manifest(store(), body)


@router.post('/splits', status_code=201)
def split_bank(body: SplitRequest):
    return create_splits(store(), body)


@router.post('/exposures')
def exposures(body: ExposureRequest):
    return add_exposures(store(), body)


@router.get('/manifests/{manifest_id}/export')
def export_manifest(manifest_id: str):
    return Response(json.dumps(get_manifest(store(), manifest_id), ensure_ascii=False), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{manifest_id}.json"'})


@router.post('/preflight')
def preflight(body: JobRequest, request: Request):
    return store().preflight(body, request.app.state.system_store)


@router.get('/options')
def options(request: Request):
    systems = []
    for item in request.app.state.system_store.list():
        for version in item['versions']:
            reason = None
            try:
                published_snapshot(request.app.state.system_store, item['system_id'], version['version'])
            except EvaluationFailure as exc:
                reason = str(exc)
            systems.append(dict(system_id=item['system_id'], version=version['version'], name=version['name'],
                                available=reason is None, unavailable_reason=reason))
    return dict(methods=METHODS, systems=systems)


@router.get('/jobs')
def jobs(include_archived: bool = False):
    catalog = store()
    items = catalog.jobs(include_archived=include_archived)
    for item in items:
        reconcile(catalog, item['job_id'])
    return {'items': catalog.jobs(include_archived=include_archived)}


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


@router.get('/jobs/{job_id}/monitor')
def job_monitor(job_id: str):
    catalog = store(); reconcile(catalog, job_id)
    return monitor(catalog, job_id)


@router.post('/jobs/{job_id}/copy-plan')
def copy_plan(job_id: str):
    return copy_job_plan(store(), job_id)


@router.post('/jobs/{job_id}/archive')
def organize_job(job_id: str, body: ArchiveJobRequest):
    return archive_job(store(), job_id, body.archived)


@router.get('/jobs/{job_id}/compare')
def question_comparison(job_id: str, sample_id: str, repeat: int = Query(0, ge=0), view: str = Query('first', pattern='^(first|latest|all)$')):
    return compare_question(store(), job_id, sample_id, repeat, view)


@router.get('/jobs/{job_id}/results')
def results(job_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
            view: str = Query('all', pattern='^(first|latest|all)$'), status: str | None = None,
            method: str | None = None, label: str | None = None, search: str | None = Query(None, max_length=500)):
    return dict(**result_rows(store(), job_id, offset, limit, view=view, status=status, method=method, label=label, search=search), offset=offset, limit=limit)


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


@router.get('/jobs/{job_id}/report')
def research_export(job_id: str, format: str = Query('json', pattern='^(json|markdown|pairs_csv|zip)$')):
    from src.evaluation.platform_reports import markdown_report, paired_csv
    catalog = store()
    job = catalog.job(job_id)
    if not job['report'] or not job['report'].get('research'):
        raise HTTPException(404, '此任务尚无研究报告；历史任务仍可导出原始结果。')
    if format == 'json':
        body, mime, extension = json.dumps(job, ensure_ascii=False), 'application/json', 'json'
    elif format == 'markdown':
        body, mime, extension = markdown_report(job), 'text/markdown', 'md'
    elif format == 'pairs_csv':
        body, mime, extension = paired_csv(job), 'text/csv', 'csv'
    else:
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('report.md', markdown_report(job))
            archive.writestr('paired-differences.csv', paired_csv(job))
            archive.writestr('summary.json', json.dumps(job, ensure_ascii=False))
            archive.writestr('frozen-plan.json', json.dumps(catalog.frozen(job_id)[1], ensure_ascii=False))
            archive.writestr('per-question.csv', exported_csv(result_rows(catalog, job_id)['items']))
        body, mime, extension = output.getvalue(), 'application/zip', 'zip'
    return Response(body, media_type=mime,
                    headers={'Content-Disposition': f'attachment; filename="{job_id}-report.{extension}"'})
