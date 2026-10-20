"""Explicit real-model API acceptance against an isolated local server.

python scripts/stage42_acceptance.py --allow-model-calls
The server must use isolated MAQA_* storage directories. No browser automation.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--output', type=Path, default=Path('.pytest-live-stage42/api-results.json'))
    parser.add_argument('--allow-model-calls', action='store_true', required=True)
    parser.add_argument('--name-suffix', default='')
    args = parser.parse_args()
    report = {'date': datetime.now().astimezone().isoformat(), 'browser_acceptance': 'deferred_by_user',
              'systems': [], 'runs': [], 'checks': {}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def persist():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with httpx.Client(base_url=args.base_url, timeout=300) as client:
        def request(method, path, **kw):
            response = client.request(method, path, **kw)
            response.raise_for_status()
            return response.json()
        try:
            defaults = {t['agent_type']: t['defaults'] for t in request('GET', '/api/v1/agent-templates')['items']}
            sources = []
            for name, content in [('Stage42 geography', b'Mara Vale founded Cedar Institute. Cedar Institute is located in Northbridge.'),
                                  ('Stage42 history', b'Cedar Institute was founded in 2008.')]:
                sources.append(request('POST', '/api/v1/knowledge/sources', data={'name': name},
                    files=[('files', ('notes.txt', content, 'text/plain'))])['source_id'])
            def agent(role, key, source=None):
                value = deepcopy(defaults[role])
                value.update(agent_id=key, name=key, source_id=source, source_version=1 if source else None)
                return value
            def create(name, template, agents, mode='sequential'):
                name += args.name_suffix
                fields = {'name': name, 'workflow_id': template, 'agents': agents, 'execution_mode': mode}
                item = request('POST', '/api/v1/systems', json=fields)
                report['systems'].append({'system_id': item['system_id'], 'name': name, 'fields': fields})
                persist()
                return item, fields
            def run(label, item, question, expected, version=None):
                selection = {'system_id': item['system_id'], **({'system_version': version} if version else {'draft_revision': item['draft_revision']})}
                response = client.post('/api/v1/qa/stream', json={'question': question, **selection})
                response.raise_for_status()
                events = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
                run_id = events[0]['run_id']
                detail = request('GET', f'/api/v1/runs/{run_id}')
                record = detail['run']
                answer = (record['final_state'] or {}).get('answer', '')
                report['runs'].append({'label': label, **detail})
                persist()
                assert record['status'] == 'completed', (label, record['error'])
                assert all(word.lower() in answer.lower() for word in expected), (label, answer)
                assert record['final_state']['verified'], (label, record['final_state']['verification_note'])
                usage = record['metrics']['agent_usage']
                assert usage and sum(v['call_count'] for v in usage.values()) == record['metrics']['llm_call_count']
                if record['metrics']['total_tokens'] is not None:
                    assert sum(v['total_tokens'] or 0 for v in usage.values()) == record['metrics']['total_tokens']
                assert [e['sequence'] for e in detail['events']] == list(range(len(detail['events'])))
                assert detail['events'][-1]['event_type'] == 'run_completed'
                assert record['context']['config_snapshot']['system_ref']['system_id'] == item['system_id']
                live_result = events[-1]['payload']['result']
                assert live_result == record['final_state']
                print(json.dumps({'label': label, 'run_id': run_id, 'answer': answer, 'calls': record['metrics']['llm_call_count']}, ensure_ascii=False), flush=True)
                return detail
            pipeline, pipeline_fields = create('阶段4.2 · 自定义流水线 API 验收', 'multi_hop_qa_fixed',
                [agent('planner', 'research_planner'), agent('retriever', 'notes_search', sources[0]),
                 agent('answer_validator', 'evidence_reviewer')])
            run('pipeline_saved_draft', pipeline, 'In which city is Cedar Institute located?', ['Northbridge'])
            request('POST', f"/api/v1/systems/{pipeline['system_id']}/versions", json={'expected_revision': 1})
            run('pipeline_published_v1', pipeline, 'In which city is Cedar Institute located?', ['Northbridge'], 'v1')
            team, fields = create('阶段4.2 · 双专家 API 验收', 'multi_expert',
                [agent('planner', 'team_planner'), agent('expert', 'geography_expert', sources[0]),
                 agent('expert', 'history_expert', sources[1]), agent('answer_validator', 'team_reviewer')])
            question = 'In which city is Cedar Institute located, and in what year was it founded?'
            run('team_sequential_draft', team, question, ['Northbridge', '2008'])
            old_version = request('POST', f"/api/v1/systems/{team['system_id']}/versions", json={'expected_revision': 1})
            fields['execution_mode'] = 'parallel'
            fields['agents'][1]['name'] = 'Geography expert · parallel version'
            updated = request('PUT', f"/api/v1/systems/{team['system_id']}/draft", json={**fields, 'expected_revision': 1})
            request('POST', f"/api/v1/systems/{team['system_id']}/versions", json={'expected_revision': 2})
            detail = run('team_parallel_published_v2', updated, question, ['Northbridge', '2008'], 'v2')
            events = detail['events']
            starts = {e['node']: e['sequence'] for e in events if e['event_type'] == 'node_started'}
            ends = {e['node']: e['sequence'] for e in events if e['event_type'] == 'node_completed'}
            assert max(starts[k] for k in ('geography_expert', 'history_expert')) < min(ends[k] for k in ('geography_expert', 'history_expert'))
            assert starts['team_reviewer'] > max(ends[k] for k in ('geography_expert', 'history_expert'))
            assert len([e for e in events if e['node'] == 'team_reviewer' and e['event_type'] == 'node_started']) == 1
            assert request('GET', f"/api/v1/systems/{team['system_id']}/versions/v1") == old_version
            exported = client.get('/api/v1/exports/runs.jsonl', params={'system_id': team['system_id']})
            exported.raise_for_status()
            assert len(exported.text.splitlines()) == 2
            for r in report['runs']:
                snapshot = r['run']['context']['config_snapshot']
                for expert_id, result in r['run']['final_state'].get('expert_results', {}).items():
                    source = snapshot['sources'][expert_id]
                    for group in result['evidence_chunks']:
                        assert all(c['source_id'] == source['source_id'] and c['source_version'] == source['version'] for c in group['chunks'])
                for source in snapshot['sources'].values():
                    assert hashlib.sha256(Path(source['resolved_path']).read_bytes()).hexdigest() == source['sha256']
                for citation in r['run']['final_state']['supporting_evidence']:
                    source = next(s for s in snapshot['sources'].values()
                        if s['source_id'] == citation['source_id'] and s['version'] == citation['source_version'])
                    corpus = json.loads(Path(source['resolved_path']).read_text(encoding='utf-8'))
                    chunk = next(c for c in corpus['chunks'] if c['chunk_id'] == citation['chunk_id'])
                    assert all(citation[key] == chunk[key] for key in
                        ('text', 'document_id', 'chunk_id', 'filename', 'line_start', 'line_end', 'char_start', 'char_end'))
            report['checks'].update(expected_answers=True, sse_history_parity=True, pinned_sources=True,
                parallel_overlap=True, single_reviewer_join=True, immutable_v1=True, scoped_export=True,
                per_instance_usage_persisted=True)
            report['checks']['citations_match_original_chunks'] = True
            report['status'] = 'passed'
        except Exception as exc:
            report['status'] = 'failed'
            # Do not serialize HTTP headers, credentials or raw provider errors.
            report['failure_type'] = type(exc).__name__
            persist()
            raise
        persist()


if __name__ == '__main__':
    main()
