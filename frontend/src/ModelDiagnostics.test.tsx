import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import ModelDiagnostics, { ModelCalls, RequestErrorDetails } from './ModelDiagnostics'
import { runDiagnosticSummary } from './diagnosticSummary'
import { blockedCall, diagnosticDetail } from './test/diagnosticFixtures'

afterEach(cleanup)

it('shows recorded provider diagnostics and offers copying the request ID', () => {
  render(<ModelCalls calls={[blockedCall]} />)
  expect(screen.getByText('供应商内容审核拦截')).toBeInTheDocument()
  expect(screen.getByText('400')).toBeInTheDocument()
  expect(screen.getByText('data_inspection_failed')).toBeInTheDocument()
  expect(screen.getByText('request-blocked-001')).toBeInTheDocument()
  expect(screen.getByText('当时未安排请求重试。')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '复制 Request ID' })).toBeInTheDocument()
})

it('keeps missing historical details unknown and does not infer a content filter from HTTP 400', () => {
  const { rerender } = render(<ModelCalls calls={[{ ...blockedCall, request_error: undefined, will_retry: undefined }]} />)
  expect(screen.getByText(/无法确定具体原因/)).toBeInTheDocument()
  expect(screen.queryByText('供应商内容审核拦截')).not.toBeInTheDocument()
  rerender(<RequestErrorDetails error={{ http_status: 400 }} />)
  expect(screen.getByText('原因未记录')).toBeInTheDocument()
  expect(screen.queryByText('供应商内容审核拦截')).not.toBeInTheDocument()
})

it('separates request retries from structured output repairs', () => {
  render(<ModelDiagnostics diagnostics={{ retry_count: 2, calls: [
    { ...blockedCall, will_retry: true, retry_delay_seconds: 1, request_error: { category: 'connection_error' } },
    { attempt: 2, output_attempt: 1, duration_ms: 100, output_chars: 10, outcome: 'invalid_schema',
      validation_errors: [{ path: 'answer', code: 'string_type', message: '必须是字符串。' }] },
    { attempt: 3, output_attempt: 2, duration_ms: 100, output_chars: 30, outcome: 'ok' },
  ] }} />)
  expect(screen.getByText(/模型调用 3 次 · 输出重试 1 次/)).toBeInTheDocument()
  expect(screen.getByText(/当时安排请求重试，等待 1 秒/)).toBeInTheDocument()
  expect(screen.getByText('answer')).toBeInTheDocument()
  expect(screen.getByText(/必须是字符串/)).toBeInTheDocument()
})

it('requires an observed later success before labelling a planned retry as recovered', () => {
  const planned = { ...blockedCall, will_retry: true }
  const noLaterSuccess = diagnosticDetail('completed', [planned]).run
  expect(runDiagnosticSummary(noLaterSuccess).recovered).toBe(false)
  const recovered = diagnosticDetail('completed', [planned,
    { attempt: 2, duration_ms: 100, output_chars: 20, outcome: 'ok' }]).run
  expect(runDiagnosticSummary(recovered)).toMatchObject({ heading: '重试后完成', recovered: true })
  expect(runDiagnosticSummary(diagnosticDetail().run)).toMatchObject({ heading: '任务失败', recovered: false })
})

it('does not mark answer mismatches or insufficient evidence as request failures', () => {
  expect(runDiagnosticSummary({ status: 'completed', labels: ['answer_mismatch', 'gold_support_not_fully_retrieved'] }).hasIssues).toBe(false)
})

it('renders provider messages as text', () => {
  const { container } = render(<RequestErrorDetails error={{ ...blockedCall.request_error, message: '<img src=x onerror=alert(1)>' }} />)
  expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
  expect(container.querySelector('img')).not.toBeInTheDocument()
})
