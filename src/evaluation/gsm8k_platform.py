"""Platform bridge to the separately versioned GSM8K reproduction core.

The core owns generation, scoring, run recovery, and the shared campaign budget.
This module never implements those algorithms or replays an unknown request.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from threading import Thread
from uuid import uuid4

from src.evaluation.experiment_config import ROOT, atomic_json, digest
from src.evaluation.platform_store import EvaluationFailure, EvaluationStore

METHODS = {'single': 1, 'reflection': 2, 'majority': 6, 'debate': 6}
FIXED = {'debug': 'train_debug_10.json', 'calibration': 'train_calibration_30.json', 'formal': 'questions_100.json'}
FIXED_HASHES = {
    'debug': '69b9562ad8f6f7601848f6422a18182e205fa813fd2633f835a2f7b6dae9cc66',
    'calibration': 'af1bf2a45dded5ff5c1a2150116f4b54fb5ad29daec9176145af52fbf0ac50e0',
    'formal': 'c6fe712c696ce690f0b7df3e7792f465869c991aa7bd7d39db4bbe2f11e6d01c',
}
ADAPTER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_threads = {}


def core_root():
    path = Path(os.environ.get('GSM_REPRO_ROOT', ROOT.parent / 'multiagentQa_use' / 'gsm8k_reproduction')).resolve()
    if not (path / 'gsm_repro' / '__main__.py').is_file():
        raise EvaluationFailure('未找到 GSM8K 复现核心，请配置后端环境变量 GSM_REPRO_ROOT。', 503)
    return path


def python_path(root):
    default = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    path = Path(os.environ.get('GSM_REPRO_PYTHON', default))
    if not path.is_file():
        raise EvaluationFailure('未找到复现核心的 Python 环境，请配置 GSM_REPRO_PYTHON。', 503)
    return path


def config_path(root, formal=False):
    if formal:
        value = Path(os.environ.get('GSM_REPRO_FORMAL_CONFIG', ROOT / 'experiments' / 'gsm8k-formal-v1-20261110.json'))
        if not value.is_file():
            raise EvaluationFailure('正式测试须先完成第六步并配置 GSM_REPRO_FORMAL_CONFIG。', 409)
        return value
    default = root / 'configs' / 'step4-20261110-134521' / 'main-single.json'
    path = Path(os.environ.get('GSM_REPRO_CONFIG', default))
    if not path.is_file():
        raise EvaluationFailure('未找到验证过的主模型配置，请配置 GSM_REPRO_CONFIG。', 503)
    return path


def jobs_root():
    return EvaluationStore().root / 'gsm8k'


def job_dir(job_id):
    if not re.fullmatch(r'gsm_[0-9a-f]{32}', job_id):
        raise EvaluationFailure('复现实验不存在。', 404)
    directory = jobs_root() / job_id
    if not (directory / 'core' / 'profile.json').is_file():
        raise EvaluationFailure('复现实验不存在。', 404)
    return directory


def cli(root, *args, env=None):
    command = [str(python_path(root)), '-B', '-m', 'gsm_repro', *map(str, args)]
    kwargs = dict(cwd=str(root), env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=1800)
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(command, **kwargs)
    try:
        output = json.loads(result.stdout)
    except ValueError:
        output = {}
    if output.get('error'):
        raise EvaluationFailure(str(output['error']), 409)
    if result.returncode or not output:
        raise EvaluationFailure('复现核心执行失败，请检查核心环境和运行记录。', 409)
    return output


def _environment(real):
    env = os.environ.copy()
    env['GSM_REPRO_DISABLE_MODEL_CALLS'] = '0' if real else '1'
    if real:
        env['GSM_REPRO_API_KEY'] = env.get('GSM_REPRO_API_KEY') or env.get('DEEPSEEK_API_KEY', '')
        env['GSM_REPRO_BASE_URL'] = env.get('GSM_REPRO_BASE_URL') or env.get('DEEPSEEK_BASE_URL', '')
        if not env['GSM_REPRO_API_KEY'] or not env['GSM_REPRO_BASE_URL']:
            raise EvaluationFailure('请先在后端环境配置模型 API Key 和 Base URL。')
    return env


def frontend_fingerprint():
    root = ROOT / 'frontend'
    paths = list((root / 'src').rglob('*.ts')) + list((root / 'src').rglob('*.tsx')) + list((root / 'src').rglob('*.css'))
    paths += [root / name for name in ('package.json', 'package-lock.json', 'vite.config.ts', 'tsconfig.app.json', 'tsconfig.node.json')]
    hasher = hashlib.sha256()
    for path in sorted(set(p for p in paths if p.is_file())):
        relative = path.relative_to(ROOT).as_posix().encode('utf-8')
        content = path.read_bytes()
        hasher.update(len(relative).to_bytes(8, 'big')); hasher.update(relative)
        hasher.update(len(content).to_bytes(8, 'big')); hasher.update(content)
    return hasher.hexdigest()


def core_identity(root):
    command = [str(python_path(root)), '-B', '-c',
               'import json;from gsm_repro.engine import identity;print(json.dumps(identity("."),ensure_ascii=True,sort_keys=True))']
    options = dict(cwd=str(root), env=_environment(False), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(command, **options)
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise EvaluationFailure('无法核对复现核心环境指纹。', 409) from exc
    if result.returncode:
        raise EvaluationFailure('无法核对复现核心环境指纹。', 409)
    return value


def verify_formal_lock(root, config):
    conditions = Path(os.environ.get('GSM_REPRO_CONDITIONS', ROOT / 'docs' / '复现正式条件-v1-20261110.lock.json'))
    try:
        raw = conditions.read_bytes()
        document = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise EvaluationFailure('正式条件文件缺失或无法读取，请完成第六步。', 409) from exc
    if hashlib.sha256(raw).hexdigest() != config.get('conditions_sha256'):
        raise EvaluationFailure('正式条件文件指纹不匹配，请重新冻结。', 409)
    if document.get('schema_version') != 1 or document.get('purpose') != 'gsm8k-formal-freeze':
        raise EvaluationFailure('正式条件文件格式无效。', 409)
    if document.get('formal_config') != {k: v for k, v in config.items() if k != 'conditions_sha256'}:
        raise EvaluationFailure('正式模型配置与冻结条件不一致。', 409)
    from src.evaluation.experiment import identity as platform_identity
    if (document.get('core_identity') != core_identity(root) or
            document.get('platform_identity') != platform_identity() or
            document.get('frontend_sha256') != frontend_fingerprint()):
        raise EvaluationFailure('代码或依赖与正式冻结版本不一致，请重新冻结后创建实验。', 409)
    if document.get('test_manifest_sha256') != FIXED_HASHES['formal']:
        raise EvaluationFailure('正式测试清单与冻结条件不一致。', 409)


def _manifest(root, selection, records):
    if selection in FIXED:
        try:
            doc = json.loads((root / 'manifests' / FIXED[selection]).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise EvaluationFailure('固定题目清单缺失或无法读取。', 409) from exc
        if doc.get('manifest_sha256') != FIXED_HASHES[selection]:
            raise EvaluationFailure('固定题目清单与已验证版本不一致，不能创建实验。', 409)
        return doc
    if selection != 'custom' or not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise EvaluationFailure('请输入 1～100 道带数字参考答案的训练题。')
    # A development experiment must not silently expose the fixed formal test questions.
    test = _manifest(root, 'formal', None)
    protected = {r['question_sha256'] for r in test['records']}
    rows, seen = [], set()
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise EvaluationFailure(f'第 {index + 1} 题必须是对象。')
        question, answer = item.get('question'), item.get('answer')
        if (not isinstance(question, str) or not question.strip() or len(question) > 5000 or
                not isinstance(answer, str) or not answer.strip() or len(answer) > 10000):
            raise EvaluationFailure(f'第 {index + 1} 题需要问题和数字参考答案。')
        question, answer = question.strip(), answer.strip()
        sha = hashlib.sha256(question.encode()).hexdigest()
        if sha in seen or sha in protected:
            raise EvaluationFailure('题目重复或包含正式测试题，不能用于开发调试。')
        seen.add(sha)
        if '####' not in answer:
            answer = '#### ' + answer
        rows.append(dict(qid=f'custom_{index + 1:04d}', split='train', source_line=index + 1,
                         question=question, answer=answer, question_sha256=sha))
    content = dict(schema_version=1, split='train', purpose='debug', dataset_sha256=digest(records),
                   sampling=dict(algorithm='user-supplied', seed=None),
                   questions_sha256=hashlib.sha256('\n'.join(r['question'] for r in rows).encode()).hexdigest(), records=rows)
    return {**content, 'manifest_sha256': digest(content)}


def create_job(name, methods, selection, count, records, fake=False):
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != ADAPTER_SHA256:
        raise EvaluationFailure('平台适配代码已变化，请重启后端再创建实验。', 409)
    root = core_root()
    if not name.strip() or len(name) > 100 or not methods or len(methods) != len(set(methods)) or set(methods) - METHODS.keys():
        raise EvaluationFailure('实验名称或复现方法无效。')
    manifest = _manifest(root, selection, records)
    if type(count) is not int or not 1 <= count <= len(manifest['records']):
        raise EvaluationFailure('题目数量超出清单范围。')
    formal = selection == 'formal'
    if formal and (count != 100 or fake or set(methods) != set(METHODS)):
        raise EvaluationFailure('正式测试需要固定 100 题和四组方法，不允许使用假模型。')
    source_config = config_path(root, formal)
    try:
        config = json.loads(source_config.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise EvaluationFailure('无法读取复现核心配置。', 503) from exc
    config = deepcopy(config)
    if not formal:
        expected = dict(model='deepseek-v4.1-flash-1017', temperature=1, top_p=1, max_tokens=2048,
                        seed_mode='per_call', seed_base=1234, extra_body={'enable_thinking': False},
                        timeout=120, max_retries=1, retry_wait_seconds=1,
                        campaign_id='gsm8k-20261109-v1', api_key_env='GSM_REPRO_API_KEY',
                        base_url_env='GSM_REPRO_BASE_URL')
        if any(config.get(key) != value for key, value in expected.items()) or config.get('stream', False):
            raise EvaluationFailure('主模型配置与训练验证条件不一致，请核对 GSM_REPRO_CONFIG。', 409)
    if formal:
        if (config.get('stage') != 'formal' or not config.get('parameters_verified') or
                config.get('methods') != methods or config.get('repeats') != 1 or
                config.get('backend') != 'openai_compatible'):
            raise EvaluationFailure('正式模型配置尚未冻结或验证。')
        verify_formal_lock(root, config)
    else:
        config.update(methods=methods, repeats=1, stage='development', backend='fake' if fake else 'openai_compatible')
    if not fake and not config.get('pricing', {}).get('confirmed'):
        raise EvaluationFailure('请先核实主模型价格和计费条件，再创建真实模型实验。')
    job_id = 'gsm_' + uuid4().hex
    directory = jobs_root() / job_id
    directory.mkdir(parents=True, exist_ok=False)
    atomic_json(directory / 'input-manifest.json', manifest)
    atomic_json(directory / 'input-config.json', config)
    atomic_json(directory / 'platform.json', dict(name=name.strip(), selection=selection, fake=fake, state='draft', error=None,
                                                   adapter_sha256=ADAPTER_SHA256))
    plan_args = ['plan', '--config', directory / 'input-config.json', '--manifest', directory / 'input-manifest.json',
                 '--run-dir', directory / 'core']
    if not formal:
        plan_args.extend(['--limit', count])
    cli(root, *plan_args, env=_environment(False))
    # Keep platform metadata separate; the core owns every request and result.
    return view_job(job_id)


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise EvaluationFailure('实验仍在运行，请稍后再操作。', 409) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _metadata(directory):
    return json.loads((directory / 'platform.json').read_text(encoding='utf-8'))


def _state(directory, state, error=None):
    doc = _metadata(directory)
    doc.update(state=state, error=error)
    atomic_json(directory / 'platform.json', doc)


def _read_db(directory):
    path = directory / 'core' / 'results.sqlite3'
    if not path.is_file():
        raise EvaluationFailure('复现实验账本缺失，请保留记录检查。', 409)
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        tasks = [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY rowid')]
        calls = [dict(r) for r in db.execute('SELECT * FROM calls ORDER BY rowid')]
    return tasks, calls


def _core_busy(directory):
    try:
        with _lock(directory / 'core' / '.execution.lock'):
            return False
    except EvaluationFailure:
        return True


def view_job(job_id, *, include_calls=False):
    directory = job_dir(job_id)
    meta = _metadata(directory)
    profile = json.loads((directory / 'core' / 'profile.json').read_text(encoding='utf-8'))
    tasks, calls = _read_db(directory)
    counts = {k: sum(t['status'] == k for t in tasks) for k in ('pending', 'running', 'completed', 'failed', 'interrupted')}
    state = meta['state']
    if state in ('running', 'stopping') and not _threads.get(job_id) and not _core_busy(directory):
        state = 'interrupted'
    results = [json.loads(t['result_json']) for t in tasks if t['result_json']]
    report_file = directory / 'core' / 'report.json'
    report = json.loads(report_file.read_text(encoding='utf-8')) if report_file.is_file() else None
    data = dict(job_id=job_id, name=meta['name'], state=state, error=meta.get('error'), fake=meta['fake'],
                selection=meta['selection'], methods=profile['config']['methods'], model=profile['config']['model'],
                samples=len(profile['selection']), planned_calls=len(profile['seed_plan']), counts=counts,
                pricing=profile['config']['pricing'], budget_cny=profile['config']['budget_cny'],
                actual_calls=len(calls), results=results, report=report, profile_sha256=profile['profile_sha256'])
    if include_calls:
        data['calls'] = [{**{k: c[k] for k in ('task_key', 'agent', 'round', 'attempt', 'status', 'duration_ms')},
                          'request': json.loads(c['request_json']),
                          'response': json.loads(c['response_json']) if c['response_json'] else None,
                          'error': json.loads(c['error_json']) if c['error_json'] else None} for c in calls]
    return data


def list_jobs():
    root = jobs_root()
    if not root.exists():
        return []
    items = []
    for d in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if d.is_dir() and re.fullmatch(r'gsm_[0-9a-f]{32}', d.name) and (d / 'platform.json').is_file():
            try:
                items.append(view_job(d.name))
            except (ValueError, OSError, sqlite3.Error, EvaluationFailure):
                continue
    return items


def _worker(job_id, root, directory, env, guard):
    try:
        while not (directory / 'stop.requested').exists():
            args = ['run', '--run-dir', directory / 'core', '--max-tasks', 1, '--allow-model-calls']
            if _metadata(directory)['selection'] == 'formal':
                args.append('--allow-test-evaluation')
            result = cli(root, *args, env=env)
            counts = result['counts']
            if result.get('blocked') or counts['failed'] or counts['interrupted']:
                _state(directory, 'interrupted', result.get('blocked') or '存在失败或未确认任务，请检查记录。')
                return
            if counts['pending'] == 0:
                _state(directory, 'completed')
                return
        _state(directory, 'paused')
    except Exception as exc:
        _state(directory, 'interrupted', str(exc))
    finally:
        _threads.pop(job_id, None)
        guard.__exit__(None, None, None)


def start_job(job_id, allow_model_calls):
    directory = job_dir(job_id)
    meta = _metadata(directory)
    if not meta['fake'] and not allow_model_calls:
        raise EvaluationFailure('启动会调用真实模型并产生费用，请先确认。')
    if meta['state'] == 'completed':
        raise EvaluationFailure('实验已完成。', 409)
    if (meta.get('adapter_sha256') != ADAPTER_SHA256 or
            ADAPTER_SHA256 != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
        raise EvaluationFailure('平台适配代码已变化，请保留旧记录并创建新实验。', 409)
    root = core_root()
    env = _environment(not meta['fake'])
    if not meta['fake']:
        reference = Path(os.environ.get('GSM_REPRO_REFERENCE_ENDPOINT', root / 'runs' / 'step4-20261110-134521' / 'main-train10' / 'endpoint.json'))
        if not reference.is_file():
            raise EvaluationFailure('缺少训练验证的端点指纹，请配置 GSM_REPRO_REFERENCE_ENDPOINT。', 409)
        try:
            expected_endpoint = json.loads(reference.read_text(encoding='utf-8'))['endpoint_sha256']
        except (OSError, ValueError, KeyError) as exc:
            raise EvaluationFailure('训练验证的端点指纹无法读取。', 409) from exc
        if digest(env['GSM_REPRO_BASE_URL'].rstrip('/')) != expected_endpoint:
            raise EvaluationFailure('当前模型端点与训练验证端点不同，请先核对后端配置。', 409)
    if meta['selection'] == 'formal':
        profile = json.loads((directory / 'core' / 'profile.json').read_text(encoding='utf-8'))
        verify_formal_lock(root, profile['config'])
    guard = _lock(directory / 'supervisor.lock')
    guard.__enter__()
    try:
        if _core_busy(directory):
            raise EvaluationFailure('已有核心进程正在处理该实验。', 409)
        tasks, calls = _read_db(directory)
        if (any(task['status'] in ('running', 'failed', 'interrupted') for task in tasks) or
                any(call['status'] in ('started', 'unknown') for call in calls)):
            raise EvaluationFailure('存在失败或未确认的请求，请先核对记录和费用，不会自动继续。', 409)
        (directory / 'stop.requested').unlink(missing_ok=True)
        _state(directory, 'running')
        thread = Thread(target=_worker, args=(job_id, root, directory, env, guard), daemon=True)
        _threads[job_id] = thread
        thread.start()
    except BaseException:
        guard.__exit__(None, None, None)
        raise
    return view_job(job_id)


def stop_job(job_id):
    directory = job_dir(job_id)
    if _metadata(directory)['state'] not in ('running', 'stopping'):
        raise EvaluationFailure('实验当前未运行。', 409)
    (directory / 'stop.requested').touch()
    _state(directory, 'stopping')
    return view_job(job_id)
