"""Platform exercises the actual reproduction core with its fake backend only."""
import json
import os
from pathlib import Path
import sqlite3
import time
from threading import Event

import pytest

from src.evaluation import gsm8k_platform as gsm
from src.evaluation.platform_store import EvaluationFailure


@pytest.fixture
def core(monkeypatch, tmp_path):
    root = Path(os.getenv('GSM_REPRO_ROOT', gsm.ROOT.parent / 'multiagentQa_use' / 'gsm8k_reproduction'))
    python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    config = root / 'configs' / 'step4-20261110-134521' / 'main-single.json'
    if not all(p.is_file() for p in (python, config, root / 'gsm_repro' / '__main__.py')):
        pytest.skip('Independent reproduction core is not installed')
    monkeypatch.setenv('GSM_REPRO_ROOT', str(root))
    monkeypatch.setenv('GSM_REPRO_PYTHON', str(python))
    monkeypatch.setenv('GSM_REPRO_CONFIG', str(config))
    monkeypatch.setenv('MAQA_EVALUATIONS_DIR', str(tmp_path / 'evaluation'))
    return root


def wait_job(job_id):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = gsm.view_job(job_id, include_calls=True)
        if result['state'] not in ('running', 'stopping'):
            return result
        time.sleep(.1)
    pytest.fail('fake reproduction worker did not finish')


def test_real_core_fake_generation_has_expected_rounds_and_scores(core):
    job = gsm.create_job('offline', list(gsm.METHODS), 'debug', 1, None, fake=True)
    assert job['counts']['pending'] == 4
    assert job['planned_calls'] == 15 and job['actual_calls'] == 0
    gsm.start_job(job['job_id'], allow_model_calls=False)
    result = wait_job(job['job_id'])
    assert result['state'] == 'completed', result['error']
    assert result['fake'] is True and result['actual_calls'] == 15
    counts = {method: sum(c['task_key'].startswith(method + ':') for c in result['calls']) for method in gsm.METHODS}
    assert counts == gsm.METHODS
    assert all('####' not in json.dumps(c['request']) for c in result['calls'])
    assert all('fixed' in item['scores'] for item in result['results'])
    core_result = [json.loads(line) for line in (gsm.job_dir(job['job_id']) / 'core' / 'results.jsonl').read_text(encoding='utf-8').splitlines()]
    assert result['results'] == core_result
    for c in result['calls']:
        if c['task_key'].startswith('debate:') and c['round'] == 1:
            peer_answers = [p['response']['content'] for p in result['calls'] if p['task_key'] == c['task_key']
                            and p['round'] == 0 and p['agent'] != c['agent']]
            assert all(answer in c['request']['messages'][-1]['content'] for answer in peer_answers)


def test_custom_questions_reject_exact_official_test_and_invalid_gold(core):
    test = json.loads((core / 'manifests' / 'questions_100.json').read_text(encoding='utf-8'))['records'][0]
    with pytest.raises(EvaluationFailure, match='正式测试题'):
        gsm.create_job('bad', ['single'], 'custom', 1, [{'question': test['question'], 'answer': '1'}], fake=True)
    with pytest.raises(EvaluationFailure):
        gsm.create_job('bad gold', ['single'], 'custom', 1, [{'question': 'Offline question', 'answer': 'not numeric'}], fake=True)


def test_fixed_manifest_fingerprint_must_match_validated_version(tmp_path):
    directory = tmp_path / 'manifests'
    directory.mkdir()
    (directory / gsm.FIXED['debug']).write_text('{"manifest_sha256":"wrong"}', encoding='utf-8')
    with pytest.raises(EvaluationFailure, match='清单与已验证版本不一致'):
        gsm._manifest(tmp_path, 'debug', None)


def test_missing_credentials_prevent_real_start_without_creating_calls(core, monkeypatch):
    for key in ('GSM_REPRO_API_KEY', 'GSM_REPRO_BASE_URL', 'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL'):
        monkeypatch.delenv(key, raising=False)
    job = gsm.create_job('real', ['single'], 'debug', 1, None)
    with pytest.raises(EvaluationFailure, match='确认'):
        gsm.start_job(job['job_id'], allow_model_calls=False)
    with pytest.raises(EvaluationFailure, match='API Key'):
        gsm.start_job(job['job_id'], allow_model_calls=True)
    monkeypatch.setenv('GSM_REPRO_API_KEY', 'fake-not-a-real-key')
    monkeypatch.setenv('GSM_REPRO_BASE_URL', 'https://not-the-validated-endpoint.invalid/v1')
    with pytest.raises(EvaluationFailure, match='端点'):
        gsm.start_job(job['job_id'], allow_model_calls=True)
    assert gsm.view_job(job['job_id'])['actual_calls'] == 0


def test_formal_evaluation_requires_frozen_config(core, monkeypatch):
    monkeypatch.setenv('GSM_REPRO_FORMAL_CONFIG', str(core / 'configs' / 'no-such-formal-config.json'))
    with pytest.raises(EvaluationFailure, match='第六步'):
        gsm.create_job('formal', list(gsm.METHODS), 'formal', 100, None)
    with pytest.raises(EvaluationFailure, match='不允许使用假模型'):
        gsm.create_job('formal fake', list(gsm.METHODS), 'formal', 100, None, fake=True)


def test_formal_lock_rejects_config_and_code_drift(core, monkeypatch, tmp_path):
    import hashlib
    from src.evaluation.experiment import identity as platform_identity
    config = json.loads((core / 'configs' / 'step4-20261110-134521' / 'main-single.json').read_text(encoding='utf-8'))
    config.update(stage='formal', parameters_verified=True, methods=list(gsm.METHODS))
    config.pop('conditions_sha256')
    lock = {'schema_version': 1, 'purpose': 'gsm8k-formal-freeze', 'formal_config': config,
            'core_identity': gsm.core_identity(core), 'platform_identity': platform_identity(),
            'frontend_sha256': gsm.frontend_fingerprint(), 'test_manifest_sha256': gsm.FIXED_HASHES['formal']}
    path = tmp_path / 'formal.json'
    raw = json.dumps(lock, sort_keys=True).encode()
    path.write_bytes(raw)
    monkeypatch.setenv('GSM_REPRO_CONDITIONS', str(path))
    cfg = {**config, 'conditions_sha256': hashlib.sha256(raw).hexdigest()}
    gsm.verify_formal_lock(core, cfg)
    monkeypatch.setattr(gsm, 'frontend_fingerprint', lambda: 'changed-on-disk')
    gsm.verify_formal_lock(core, cfg)
    with pytest.raises(EvaluationFailure, match='模型配置'):
        gsm.verify_formal_lock(core, {**cfg, 'temperature': 0})
    lock['frontend_sha256'] = 'changed'
    raw = json.dumps(lock, sort_keys=True).encode(); path.write_bytes(raw)
    changed_frontend_cfg = {**config, 'conditions_sha256': hashlib.sha256(raw).hexdigest()}
    gsm.verify_formal_lock(core, changed_frontend_cfg)
    with pytest.raises(EvaluationFailure, match='指纹不匹配'):
        gsm.verify_formal_lock(core, cfg)
    original_core = lock['core_identity']
    lock['core_identity'] = {'changed': True}
    raw = json.dumps(lock, sort_keys=True).encode(); path.write_bytes(raw)
    with pytest.raises(EvaluationFailure, match='代码或依赖'):
        gsm.verify_formal_lock(core, {**config, 'conditions_sha256': hashlib.sha256(raw).hexdigest()})
    lock['core_identity'] = original_core
    lock['platform_identity'] = {'changed': True}
    raw = json.dumps(lock, sort_keys=True).encode(); path.write_bytes(raw)
    with pytest.raises(EvaluationFailure, match='代码或依赖'):
        gsm.verify_formal_lock(core, {**config, 'conditions_sha256': hashlib.sha256(raw).hexdigest()})


def test_frontend_version_is_recorded_at_creation_and_start(core, monkeypatch):
    current = {'sha': 'a' * 64}
    monkeypatch.setattr(gsm, 'frontend_fingerprint', lambda: current['sha'])
    job = gsm.create_job('frontend audit', ['single'], 'debug', 1, None, fake=True)
    assert [x['sha256'] for x in job['frontend_observations']] == ['a' * 64]
    current['sha'] = 'b' * 64
    gsm.start_job(job['job_id'], allow_model_calls=False)
    result = wait_job(job['job_id'])
    assert result['state'] == 'completed'
    assert [(x['event'], x['sha256']) for x in result['frontend_observations']] == [
        ('created', 'a' * 64), ('started', 'b' * 64)]
    assert all(x['observed_at'] for x in result['frontend_observations'])


def test_unreadable_frontend_is_recorded_without_blocking(core, monkeypatch):
    def unavailable():
        raise OSError('offline unavailable')
    monkeypatch.setattr(gsm, 'frontend_fingerprint', unavailable)
    job = gsm.create_job('missing frontend', ['single'], 'debug', 1, None, fake=True)
    gsm.start_job(job['job_id'], allow_model_calls=False)
    result = wait_job(job['job_id'])
    assert result['state'] == 'completed'
    assert [x['sha256'] for x in result['frontend_observations']] == [None, None]


def test_changed_platform_adapter_refuses_to_start(core):
    job = gsm.create_job('drift', ['single'], 'debug', 1, None, fake=True)
    path = gsm.job_dir(job['job_id']) / 'platform.json'
    metadata = json.loads(path.read_text(encoding='utf-8'))
    metadata['adapter_sha256'] = 'different-version'
    path.write_text(json.dumps(metadata), encoding='utf-8')
    with pytest.raises(EvaluationFailure, match='适配代码已变化'):
        gsm.start_job(job['job_id'], allow_model_calls=False)
    assert gsm.view_job(job['job_id'])['actual_calls'] == 0


def test_unconfirmed_execution_is_not_automatically_resumed(core):
    job = gsm.create_job('unknown', ['single'], 'debug', 1, None, fake=True)
    database = gsm.job_dir(job['job_id']) / 'core' / 'results.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE tasks SET status='running'")
    with pytest.raises(EvaluationFailure, match='未确认'):
        gsm.start_job(job['job_id'], allow_model_calls=False)
    assert gsm.view_job(job['job_id'])['actual_calls'] == 0


def test_pause_waits_for_current_task_and_resume_does_not_repeat(core, monkeypatch):
    job = gsm.create_job('pause', ['single'], 'debug', 2, None, fake=True)
    started, release = Event(), Event()
    real_cli = gsm.cli
    def controlled(root, *args, **kwargs):
        if args[0] == 'run' and not started.is_set():
            started.set()
            assert release.wait(30)
        return real_cli(root, *args, **kwargs)
    monkeypatch.setattr(gsm, 'cli', controlled)
    gsm.start_job(job['job_id'], allow_model_calls=False)
    assert started.wait(20)
    gsm.stop_job(job['job_id'])
    release.set()
    paused = wait_job(job['job_id'])
    assert paused['state'] == 'paused' and paused['actual_calls'] == 1
    gsm.start_job(job['job_id'], allow_model_calls=False)
    done = wait_job(job['job_id'])
    assert done['state'] == 'completed' and done['actual_calls'] == 2
    with pytest.raises(EvaluationFailure, match='已完成'):
        gsm.start_job(job['job_id'], allow_model_calls=False)
