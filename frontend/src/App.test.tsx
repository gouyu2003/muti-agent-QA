import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { getSystemInfo } from './api'
import type { SystemInfo } from './types'

vi.mock('./api', () => ({
  getSystemInfo: vi.fn(),
  streamQuestion: vi.fn(),
}))

const systemInfo: SystemInfo = {
  name: 'Multi-Agent QA',
  version: 'v0',
  protocol_id: 'multi_hop_qa_v0',
  model_name: 'deepseek-v4-flash-0731',
  agents: [
    { agent_id: 'planner_v0', name: '规划智能体', role: 'planner', description: '' },
    { agent_id: 'retriever_v0', name: '检索智能体', role: 'retriever', description: '' },
    {
      agent_id: 'answer_validator_v0',
      name: '回答与验证智能体',
      role: 'answer_validator',
      description: '',
    },
  ],
  flow: [],
  retry_limits: { retrieval_retry_count: 3, plan_retry_count: 2 },
  dataset: {
    name: 'HotpotQA',
    record_id: 'fixture',
    question_type: 'bridge',
    level: 'hard',
    context_count: 10,
    supporting_fact_count: 3,
    default_question: 'A multi-hop question?',
  },
}

describe('App', () => {
  beforeEach(() => {
    vi.mocked(getSystemInfo).mockResolvedValue(systemInfo)
  })

  it('renders the V0 workbench with Ant Design controls and system metadata', async () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: '观察一次真实的多智能体协作' })).toBeInTheDocument()
    expect(await screen.findByText('服务正常')).toBeInTheDocument()
    expect(screen.getByDisplayValue('A multi-hop question?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /开始协同问答/ })).toBeEnabled()
    expect(screen.getByText('hard · bridge · 10 个上下文')).toBeInTheDocument()
  })
})
