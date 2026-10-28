import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import EvaluationRunDetail from './EvaluationRunDetail'
import { blockedCall, diagnosticDetail } from './test/diagnosticFixtures'

afterEach(cleanup)

it('shows a readable failure while keeping the source JSON collapsed', () => {
  render(<EvaluationRunDetail detail={diagnosticDetail()} />)
  expect(screen.getByRole('heading', { name: '运行诊断' })).toBeInTheDocument()
  expect(screen.getByText('data_inspection_failed')).toBeInTheDocument()
  expect(screen.getByText('request-blocked-001')).toBeInTheDocument()
  expect(screen.queryByText(/"run_id": "run_1"/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('执行时间线（1 个事件）'))
  expect(screen.getByText('运行失败')).toBeInTheDocument()
  fireEvent.click(screen.getByText('原始 JSON（高级详情）'))
  expect(screen.getByText(/"run_id": "run_1"/)).toBeInTheDocument()
})

it('keeps recovered request errors visible without presenting the final task as failed', () => {
  render(<EvaluationRunDetail detail={diagnosticDetail('completed', [
    { ...blockedCall, will_retry: true, request_error: { category: 'connection_error', message: 'Connection failed.' } },
    { attempt: 2, duration_ms: 100, output_chars: 20, outcome: 'ok' },
  ])} />)
  expect(screen.getByText('流程完成')).toBeInTheDocument()
  expect(screen.getByText('重试后完成')).toBeInTheDocument()
  expect(screen.getByText('Connection failed.')).toBeInTheDocument()
  expect(screen.queryByText('任务失败')).not.toBeInTheDocument()
  expect(screen.getByText('Cedar Institute')).toBeInTheDocument()
})
