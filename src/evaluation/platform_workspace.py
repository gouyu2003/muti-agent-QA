"""Reusable immutable plans and reversible platform job organization."""
import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.evaluation.experiment_config import digest
from src.evaluation.experiment_store import utcnow
from src.evaluation.platform_store import EvaluationFailure, JobRequest


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    request: dict | None = None
    source_job_id: str | None = None
    sha256: str | None = None


def get_plan(store, key):
    with store.connection() as db:
        row = db.execute('SELECT * FROM saved_plans WHERE id=?', (key,)).fetchone()
    if not row:
        raise EvaluationFailure('实验方案不存在。', 404)
    result = dict(row)
    result['request'] = json.loads(result.pop('document'))
    if digest(result['request']) != result['sha256']:
        raise EvaluationFailure('实验方案指纹不一致。')
    return result


def save_plan(store, fields):
    if not fields.name.strip() or (fields.request is None) == (fields.source_job_id is None):
        raise EvaluationFailure('请提供方案名称，并选择当前配置或一个既有任务。')
    payload = fields.request if fields.request is not None else store.frozen(fields.source_job_id)[1]['request']
    try:
        request = JobRequest.model_validate(payload).model_dump(exclude={'preflight_token', 'saved_plan_id'})
    except ValidationError as exc:
        raise EvaluationFailure('方案格式不兼容，请使用平台导出的实验方案 JSON。') from exc
    sha = digest(request)
    if fields.sha256 and fields.sha256 != sha:
        raise EvaluationFailure('导入方案指纹不一致。')
    key = 'plan_' + uuid4().hex
    with store.connection() as db:
        db.execute('INSERT INTO saved_plans VALUES (?,?,?,?,?,?)',
                   (key, fields.name.strip(), json.dumps(request, ensure_ascii=False), sha, fields.source_job_id, utcnow()))
    return get_plan(store, key)


def copy_job_plan(store, job_id):
    _, frozen = store.frozen(job_id)
    payload = JobRequest.model_validate(frozen['request']).model_dump()
    payload.update(name=(payload['name'][:96] + ' 副本'), preflight_token=None, saved_plan_id=None, copied_from_job_id=job_id)
    return dict(request=payload, source_job_id=job_id, needs_preflight=True)


def archive_job(store, job_id, archived):
    store.frozen(job_id)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        status = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
        if status in ('queued', 'running', 'stopping'):
            raise EvaluationFailure('运行中的任务不能归档，请先暂停或等待结束。', 409)
        db.execute('INSERT INTO job_catalog VALUES (?,?) ON CONFLICT(job_id) DO UPDATE SET archived=excluded.archived',
                   (job_id, int(archived)))
    return store.job(job_id)
