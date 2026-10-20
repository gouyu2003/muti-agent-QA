"""Real-model acceptance through in-process HTTP/SSE, with isolated local storage.

python -m scripts.stage43_acceptance --allow-model-calls --model-host <approved-model-hostname>
No development service is started or restarted. Full records stay in the isolated root.
"""
from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import load_dotenv

from fastapi.testclient import TestClient
from backend.app import app
from src.runtime.protocol_store import digest


def source_fingerprint():
    paths = sorted(p for root in ('src', 'backend', 'frontend/src') for p in Path(root).rglob('*')
                   if p.is_file() and p.suffix in ('.py', '.ts', '.tsx', '.css'))
    return digest({p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--allow-model-calls', action='store_true', required=True)
    parser.add_argument('--model-host', required=True, help='Explicitly verified existing model destination hostname')
    parser.add_argument('--root', type=Path, default=Path('.pytest-live-stage43'))
    args = parser.parse_args()
    root = args.root.resolve()
    if root.parent != Path.cwd().resolve() or not root.name.startswith('.pytest-live-stage43'):
        raise SystemExit('Acceptance storage must be a .pytest-live-stage43* directory within this workspace.')
    load_dotenv()
    destination = urlsplit(os.environ.get('DEEPSEEK_BASE_URL', ''))
    if destination.scheme != 'https' or destination.hostname != args.model_host or destination.username or destination.password:
        raise SystemExit('Model destination must match the explicitly selected HTTPS hostname, without URL credentials.')
    if any((root / child).exists() for child in ('protocols', 'systems', 'knowledge', 'runs.sqlite3')):
        raise SystemExit('Use a fresh isolated root; existing catalogs must not be reused by real-model acceptance.')
    root.mkdir(parents=True, exist_ok=True)
    for name, child in [('MAQA_PROTOCOLS_DIR', 'protocols'), ('MAQA_SYSTEMS_DIR', 'systems'),
                        ('MAQA_KNOWLEDGE_DIR', 'knowledge'), ('MAQA_RUN_DB_PATH', 'runs.sqlite3'),
                        ('MAQA_EXPERIMENT_CATALOG_PATH', 'missing-experiments.json')]:
        os.environ[name] = str(root / child)
    report = dict(date=datetime.now(timezone.utc).isoformat(), code_sha256=source_fingerprint(),
        model_destination_hostname=destination.hostname, material_origin='inline_synthetic_strings_only',
        transport='in_process_HTTP_SSE', real_models=True, development_services_restarted=False,
        browser_acceptance='pending_combined_stage42_stage43', runs=[], checks={})
    def persist(): (root / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    stable_systems, stable_runs = {}, {}
    with TestClient(app) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs); response.raise_for_status(); return response.json()
        defaults = {t['agent_type']: t['defaults'] for t in request('GET', '/api/v1/agent-templates')['items']}
        templates = {t['template_id']: t['spec'] for t in request('GET', '/api/v1/protocols/templates')['items']}
        sources = []
        for label, body in [('geography', 'Cedar Institute is located in Northbridge. It was founded in 2008.'),
                            ('independent', 'An independent record states that Cedar Institute is located in Northbridge, and was founded in 2008.'),
                            ('conflicting', 'The official campus record states that Cedar Institute is located in Southbank. It was founded in 2008.')]:
            source = request('POST', '/api/v1/knowledge/sources', data={'name': f'Stage43 synthetic {label}'},
                             files=[('files', ('notes.txt', body.encode(), 'text/plain'))])
            sources.append(source['source_id'])
        def create(family, label, selected_sources):
            spec = deepcopy(templates[family])
            protocol = request('POST', '/api/v1/protocols', json=dict(name=f'Stage43 {label}', spec=spec))
            request('POST', f'/api/v1/protocols/{protocol["protocol_id"]}/versions', json={'expected_revision': 1})
            roles = {'pipeline': [('planner', 'plan_agent'), ('retriever', 'search_agent'), ('answer_validator', 'judge_agent')],
                     'lead': [('lead', 'lead_agent'), ('expert', 'expert_a'), ('expert', 'expert_b'), ('answer_validator', 'judge_agent')],
                     'debate': [('expert', 'expert_a'), ('expert', 'expert_b'), ('answer_validator', 'judge_agent')]}[family]
            agents = []
            source_index = 0
            for role, key in roles:
                agent = deepcopy(defaults[role]); agent.update(agent_id=key, name=key)
                if role in ('retriever', 'expert'):
                    agent.update(source_id=selected_sources[source_index % len(selected_sources)], source_version=1); source_index += 1
                agents.append(agent)
            bindings = {slot: [a['agent_id'] for a in agents if a['agent_type'] == contract['role']] for slot, contract in spec['slots'].items()}
            fields = dict(name=f'Stage43 {label}', agents=agents, protocol_ref=dict(protocol_id=protocol['protocol_id'], version='v1'), protocol_bindings=bindings)
            item = request('POST', '/api/v1/systems', json=fields)
            assert not item['draft']['snapshot']['configuration_errors']
            release = request('POST', f'/api/v1/systems/{item["system_id"]}/versions', json={'expected_revision': 1})
            stable_systems[item['system_id']] = request('GET', f'/api/v1/systems/{item["system_id"]}')
            return item, protocol, release
        def run(label, family, selected_sources, question, expected=None, verified=None):
            print(json.dumps({'starting': label}), flush=True)
            item, protocol, release = create(family, label, selected_sources)
            response = client.post('/api/v1/qa/stream', json=dict(question=question, system_id=item['system_id'], system_version='v1'))
            response.raise_for_status()
            envelopes = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
            run_id = envelopes[0]['run_id']
            detail = request('GET', f'/api/v1/runs/{run_id}')
            (root / f'{run_id}.json').write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding='utf-8')
            record = detail['run']; final = record['final_state'] or {}; metrics = record['metrics']
            summary = dict(label=label, family=family, run_id=run_id, system_id=item['system_id'], protocol_id=protocol['protocol_id'],
                status=record['status'], answer=final.get('answer'), verified=final.get('verified'), failure_type=final.get('failure_type'),
                consensus_status=final.get('consensus_status'), termination_reason=final.get('termination_reason'),
                config_sha256=record['context']['config_snapshot']['config_sha256'], model=record['context']['model_name'], metrics=metrics,
                calls=[{k: c[k] for k in ('call_id', 'agent_id', 'stage_id', 'round', 'task_id', 'kind', 'status')}
                       for c in sorted(final.get('protocol_calls', {}).values(), key=lambda c: c['invocation_index'])])
            report['runs'].append(summary); persist()
            assert record['status'] == 'completed', (label, record['error'])
            assert envelopes[-1]['payload']['result'] == final
            assert [e['sequence'] for e in detail['events']] == list(range(len(detail['events'])))
            if expected: assert expected.casefold() in final.get('answer', '').casefold(), (label, final.get('answer'))
            if verified is not None: assert final.get('verified') == verified, (label, final.get('verification_note'))
            calls = list(final['protocol_calls'].values())
            assert len({c['call_id'] for c in calls}) == len(calls)
            assert all(c['status'] == 'completed' for c in calls)
            assert sum(a['call_count'] for a in metrics['agent_usage'].values()) == metrics['llm_call_count']
            if metrics['total_tokens'] is not None:
                assert sum(a['total_tokens'] for a in metrics['agent_usage'].values()) == metrics['total_tokens']
            for evidence in final.get('supporting_evidence', []):
                assert evidence['source_id'] in selected_sources and evidence['source_version'] == 1 and evidence.get('text')
            if family == 'debate':
                assert len([c for c in calls if c['kind'] == 'propose']) == 2
                assert all(not c['input']['reads'] for c in calls if c['kind'] == 'propose')
                assert all(c['output'].get('critiques') for c in calls if c['kind'] == 'critique')
                assert all(c['output'].get('responses') for c in calls if c['kind'] == 'revise')
            if family == 'lead':
                lead_calls = [c for c in calls if c['kind'] == 'lead' and c['output']['action'] == 'dispatch']
                for lead in lead_calls:
                    tasks = {t['task_id']: t for t in lead['output']['tasks']}
                    work = [c for c in calls if c['kind'] == 'dispatch' and any(r['call_id'] == lead['call_id'] for r in c['input']['reads'])]
                    assert len(work) == len(tasks)
                    assert all(c['agent_id'] == tasks[c['task_id']]['agent_id'] and c['input']['task']['instruction'] == tasks[c['task_id']]['instruction'] for c in work)
            assert run_id in client.get(f'/api/v1/exports/runs.jsonl?system_id={item["system_id"]}').text
            changed = dict(name='Updated protocol', spec=deepcopy(protocol['draft']['spec']))
            changed['spec']['limits']['max_node_calls'] -= 1
            request('PUT', f'/api/v1/protocols/{protocol["protocol_id"]}/draft', json={**changed, 'expected_revision': 1})
            request('POST', f'/api/v1/protocols/{protocol["protocol_id"]}/versions', json={'expected_revision': 2})
            assert request('GET', f'/api/v1/systems/{item["system_id"]}/versions/v1') == release
            stable_runs[run_id] = digest(detail)
            summary['checks_passed'] = True; persist()
            print(json.dumps({'completed': label, 'run_id': run_id, 'answer': final.get('answer'), 'verified': final.get('verified'), 'model_calls': metrics['llm_call_count']}), flush=True)
        question = 'In which city is Cedar Institute located?'
        run('pipeline', 'pipeline', sources[:1], question, 'Northbridge', True)
        run('lead', 'lead', sources[:2], question, 'Northbridge', True)
        run('debate_agreement', 'debate', sources[:2], question, 'Northbridge', True)
        run('debate_conflict', 'debate', [sources[0], sources[2]], question, verified=False)
        run('pipeline_insufficient', 'pipeline', sources[:1], 'Who is the director of Cedar Institute?', verified=False)
    with TestClient(app) as client:
        for key, document in stable_systems.items(): assert client.get(f'/api/v1/systems/{key}').json() == document
        for key, checksum in stable_runs.items(): assert digest(client.get(f'/api/v1/runs/{key}').json()) == checksum
    report['checks'].update(sse_history_equal=True, identities_and_outputs_preserved=True, usage_equal=True,
        fixed_evidence_locations=True, protocol_upgrade_does_not_change_system_v1=True, store_reload_equal=True)
    report['passed'] = True; persist()
    print(json.dumps({'passed': True, 'cases': len(report['runs']), 'results': str(root / 'results.json')}), flush=True)


if __name__ == '__main__': main()
