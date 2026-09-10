import { describe, expect, it } from 'vitest'
import { parseSseBlock } from './api'

describe('parseSseBlock', () => {
  it('parses an event and preserves Chinese payload text', () => {
    const event = parseSseBlock(
      'event: run_failed\ndata: {"run_id":"r1","timestamp":"now","payload":{"message":"配置缺失"}}',
    )

    expect(event).toEqual({
      type: 'run_failed',
      run_id: 'r1',
      timestamp: 'now',
      payload: { message: '配置缺失' },
    })
  })

  it('ignores incomplete blocks', () => {
    expect(parseSseBlock('data: {}')).toBeNull()
    expect(parseSseBlock('event: run_started')).toBeNull()
  })
})
