"""Immutable platform sample manifests and explicit, bank-scoped exposure records."""
from collections import Counter
import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.evaluation.experiment_config import digest
from src.evaluation.experiment_data import sample
from src.evaluation.experiment_store import utcnow
from src.evaluation.platform_store import EvaluationFailure


class ManifestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bank_id: str
    bank_version: int = Field(ge=1, strict=True)
    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(pattern='^(smoke|development|evaluation)$')
    count: int | None = Field(default=None, ge=1, le=10000, strict=True)
    seed: int = Field(default=42, strict=True)
    sampling: str = Field(default='proportional', pattern='^(proportional|random)$')
    source_manifest: dict | None = None


class SplitRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bank_id: str
    bank_version: int = Field(ge=1, strict=True)
    name: str = Field(min_length=1, max_length=80)
    development: int = Field(ge=1, le=10000, strict=True)
    evaluation: int = Field(ge=1, le=10000, strict=True)
    smoke: int = Field(default=1, ge=1, le=10000, strict=True)
    seed: int = Field(default=42, strict=True)
    sampling: str = Field(default='proportional', pattern='^(proportional|random)$')


class ExposureRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bank_id: str
    bank_version: int = Field(ge=1, strict=True)
    sample_ids: list[str] = Field(default_factory=list, max_length=10000)
    source_manifest: dict | None = None
    reason: str = Field(min_length=1, max_length=1000)


def manifest_ids(source):
    samples = source.get('samples')
    if not isinstance(samples, list) or not samples:
        raise EvaluationFailure('清单需包含非空 samples 数组，每项含 sample_id。')
    ids = [row.get('sample_id') if isinstance(row, dict) else None for row in samples]
    if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
        raise EvaluationFailure('清单中的 sample_id 必须为不重复的非空字符串。')
    if source.get('sha256') and digest({k: v for k, v in source.items() if k != 'sha256'}) != source['sha256']:
        raise EvaluationFailure('导入清单指纹不一致。')
    return ids


def exposed_ids(db, bank_id):
    return {r[0] for r in db.execute('SELECT sample_id FROM exposures WHERE bank_id=?', (bank_id,))}


def register(db, bank_id, ids, reason):
    db.executemany('INSERT OR IGNORE INTO exposures VALUES (?,?,?,?)',
                   [(bank_id, key, reason, utcnow()) for key in ids])


def check_overlap(db, bank_id, ids):
    overlap = set(ids) & exposed_ids(db, bank_id)
    if overlap:
        raise EvaluationFailure(f'评价清单包含 {len(overlap)} 道开发、试跑或已登记曝光题，请使用独立评价题。')


def select(records, count, seed, purpose, strategy):
    if not count or count > len(records):
        raise EvaluationFailure('符合条件的题目数量不足。')
    if strategy == 'random':
        return sorted(records, key=lambda r: digest([seed, purpose, r['_id']]))[:count]
    if set(r['type'] for r in records) <= {'bridge', 'comparison'}:
        return sample(records, count, seed, purpose, 'proportional')
    # General question banks retain their own type labels, with proportional quotas.
    groups = {kind: [r for r in records if r['type'] == kind] for kind in sorted({r['type'] for r in records})}
    quotas = {kind: count * len(rows) // len(records) for kind, rows in groups.items()}
    for kind in sorted(groups, key=lambda k: (-(count * len(groups[k]) % len(records)), k))[:count - sum(quotas.values())]:
        quotas[kind] += 1
    selected = [row for kind, rows in groups.items() for row in
                sorted(rows, key=lambda r: digest([seed, purpose, r['_id']]))[:quotas[kind]]]
    return sorted(selected, key=lambda r: digest([seed, purpose, 'order', r['_id']]))


def document(bank, name, purpose, records, seed, strategy, origin=None):
    if not name.strip():
        raise EvaluationFailure('清单名称不能为空。')
    result = dict(manifest_id='manifest_' + uuid4().hex, name=name.strip(), purpose=purpose,
                  bank_id=bank['bank_id'], bank_version=bank['version'], bank_sha256=bank['sha256'],
                  seed=seed, sampling=strategy, count=len(records), type_counts=dict(Counter(r['type'] for r in records)),
                  samples=[dict(sample_id=r['_id'], type=r['type']) for r in records],
                  origin=origin, created_at=utcnow())
    result['sha256'] = digest(result)
    return result


def save(db, doc):
    db.execute('INSERT INTO manifests VALUES (?,?,?)', (doc['manifest_id'], json.dumps(doc, ensure_ascii=False), doc['created_at']))
    if doc['purpose'] != 'evaluation':
        register(db, doc['bank_id'], [s['sample_id'] for s in doc['samples']], f"划为{doc['purpose']}：{doc['name']}")


def get_manifest(store, manifest_id, db=None):
    if db is None:
        with store.connection() as connection:
            return get_manifest(store, manifest_id, connection)
    row = db.execute('SELECT document FROM manifests WHERE id=?', (manifest_id,)).fetchone()
    if not row:
        raise EvaluationFailure('评价清单不存在。', 404)
    doc = json.loads(row[0])
    if digest({k: v for k, v in doc.items() if k != 'sha256'}) != doc['sha256']:
        raise EvaluationFailure('保存的清单指纹不一致。')
    return doc


def import_manifest(store, fields):
    bank = store.bank(fields.bank_id, fields.bank_version)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        records = bank['records']
        if fields.source_manifest is not None:
            source = fields.source_manifest
            ids = manifest_ids(source)
            expected = source.get('dataset_sha256') or source.get('bank_sha256')
            if not expected or expected not in {bank['sha256'], bank.get('raw_sha256')}:
                raise EvaluationFailure('清单数据指纹与题库不匹配；请上传原始数据文件的固定版本。')
            if source.get('purpose') != fields.purpose:
                raise EvaluationFailure('导入清单的用途与选择的实验用途不一致。')
            indexed = {r['_id']: r for r in records}
            if not set(ids) <= indexed.keys():
                raise EvaluationFailure('导入清单包含题库中不存在的题目。')
            records = [indexed[key] for key in ids]
            if fields.count is not None:
                raise EvaluationFailure('导入固定清单不能再次抽样。')
        else:
            if fields.purpose == 'evaluation':
                excluded = exposed_ids(db, fields.bank_id)
                records = [r for r in records if r['_id'] not in excluded]
            records = select(records, fields.count or len(records), fields.seed, fields.purpose, fields.sampling)
        if fields.purpose == 'evaluation':
            check_overlap(db, fields.bank_id, [r['_id'] for r in records])
        doc = document(bank, fields.name, fields.purpose, records, fields.seed, fields.sampling, fields.source_manifest)
        save(db, doc)
    return doc


def create_splits(store, fields):
    bank = store.bank(fields.bank_id, fields.bank_version)
    if fields.smoke > fields.development:
        raise EvaluationFailure('试跑题数不能超过开发集题数。')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        development = select(bank['records'], fields.development, fields.seed, 'development', fields.sampling)
        excluded = exposed_ids(db, fields.bank_id) | {r['_id'] for r in development}
        evaluation = select([r for r in bank['records'] if r['_id'] not in excluded], fields.evaluation,
                            fields.seed, 'evaluation', fields.sampling)
        smoke = select(development, fields.smoke, fields.seed, 'smoke', fields.sampling)
        docs = [document(bank, fields.name + ' · ' + purpose, purpose, rows, fields.seed, fields.sampling)
                for purpose, rows in [('development', development), ('evaluation', evaluation), ('smoke', smoke)]]
        for doc in docs:
            save(db, doc)
    return dict(items=docs)


def add_exposures(store, fields):
    bank = store.bank(fields.bank_id, fields.bank_version)
    if not fields.reason.strip():
        raise EvaluationFailure('曝光登记原因不能为空。')
    ids = fields.sample_ids
    if fields.source_manifest is not None:
        if fields.source_manifest.get('purpose') not in ('development', 'smoke'):
            raise EvaluationFailure('曝光登记只接受开发或试跑清单。')
        expected = fields.source_manifest.get('dataset_sha256') or fields.source_manifest.get('bank_sha256')
        if expected and expected not in {bank['sha256'], bank.get('raw_sha256')}:
            raise EvaluationFailure('曝光清单的数据指纹与题库不匹配。')
        ids = manifest_ids(fields.source_manifest)
    known = {r['_id'] for r in bank['records']}
    if not ids or not set(ids) <= known:
        raise EvaluationFailure('曝光登记需指定当前题库中存在的题目 ID。')
    with store.connection() as db:
        register(db, fields.bank_id, ids, fields.reason)
        return dict(count=len(exposed_ids(db, fields.bank_id)))


def job_records(store, fields, bank):
    if fields.manifest_id:
        doc = get_manifest(store, fields.manifest_id)
        if (doc['bank_id'], doc['bank_version'], doc['bank_sha256']) != (bank['bank_id'], bank['version'], bank['sha256']):
            raise EvaluationFailure('清单绑定的题库版本与当前选择不一致。')
        if fields.sample_size is not None or doc['purpose'] != fields.purpose:
            raise EvaluationFailure('固定清单不能再次抽样，且用途必须与实验一致。')
        records = {r['_id']: r for r in bank['records']}
        selected = [records[s['sample_id']] for s in doc['samples']]
    else:
        if fields.purpose == 'evaluation':
            raise EvaluationFailure('正式评价需先选择已冻结的评价清单。')
        if fields.sample_size and fields.sample_size > len(bank['records']):
            raise EvaluationFailure('抽样数量超过问题库题数。')
        selected = sorted(bank['records'], key=lambda r: digest([fields.seed, r['_id']]))[:fields.sample_size or len(bank['records'])]
        doc = None
    if fields.purpose == 'evaluation':
        if any(r['answer'] is None for r in selected):
            raise EvaluationFailure('正式评价需为每道题提供标准答案。')
        with store.connection() as db:
            check_overlap(db, fields.bank_id, [r['_id'] for r in selected])
    return selected, doc
