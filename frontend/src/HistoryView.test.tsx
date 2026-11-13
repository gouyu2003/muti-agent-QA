import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import HistoryView, { RunDetailPanel } from './HistoryView'
import { getExperiments, getManagedSystems, getRuns, getRunStats } from './api'
import type { RunResult, StoredRunDetail } from './types'

vi.mock('./api', () => ({ getExperiments: vi.fn(), getManagedSystems: vi.fn(), getRuns: vi.fn(),
  getRunStats: vi.fn(), getRunDetail: vi.fn(), getRunsExportUrl: () => '/api/v1/exports/runs.jsonl' }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

const detail = (result: RunResult): StoredRunDetail => ({ run: {
  context: { run_id: 'chat-test', entrypoint: 'api', question: '阅读建议', workflow_id: 'library',
    source_id: null, record_id: null,
    method_id: 'library', protocol_id: 'library', model_name: 'stub', code_version: 'test', config_snapshot: {} },
  status: 'completed', started_at: '2026-11-13T08:00:00Z', finished_at: '2026-11-13T08:00:01Z',
  final_state: result, error: null, metrics: { duration_ms: 1000, node_call_count: 2, llm_call_count: 2,
    prompt_tokens: 10, completion_tokens: 10, total_tokens: 20, estimated_cost_usd: null,
    plan_retry_count: 0, retrieval_retry_count: 0, validation_retry_count: 0 },
}, events: [] })

it('shows general replies in run details without a failed verification or empty evidence section', () => {
  render(<RunDetailPanel detail={detail({ answer: '定期阅读。', verified: false, verification_applicable: false,
    response_parts: [{ kind: 'general', content: '定期阅读。' }] })} />)
  expect(screen.getByText('通用回答')).toBeInTheDocument()
  expect(screen.queryByText('验证未通过')).not.toBeInTheDocument()
  expect(screen.queryByRole('region', { name: '运行证据' })).not.toBeInTheDocument()
})

it('limits the mixed reply verdict to its knowledge section in run details', () => {
  render(<RunDetailPanel detail={detail({ answer: '30 册。建议阅读。', verified: true, verification_applicable: true,
    response_parts: [{ kind: 'knowledge', content: '30 册。' }, { kind: 'general', content: '建议阅读。' }] })} />)
  expect(within(screen.getByRole('region', { name: '知识库部分' })).getByText('验证通过')).toBeInTheDocument()
  expect(within(screen.getByRole('region', { name: '通用部分' })).queryByText('验证通过')).not.toBeInTheDocument()
})

it('shows a dash when completed runs contain no applicable verification', async () => {
  vi.mocked(getExperiments).mockResolvedValue([])
  vi.mocked(getManagedSystems).mockResolvedValue({ items: [] })
  vi.mocked(getRuns).mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 })
  vi.mocked(getRunStats).mockResolvedValue({ total_runs: 1, completed_runs: 1, failed_runs: 0, cancelled_runs: 0,
    running_runs: 0, verified_runs: 0, verification_applicable_runs: 0, verification_pass_rate: 0,
    completion_rate: 1, average_duration_ms: 1000, average_retry_count: 0, failure_types: {} })
  render(<MemoryRouter><HistoryView /></MemoryRouter>)
  await waitFor(() => expect(screen.getByText('运行总数').parentElement?.querySelector('strong')).toHaveTextContent('1'))
  expect(screen.getByText('验证通过率').parentElement?.querySelector('strong')).toHaveTextContent('—')
})
