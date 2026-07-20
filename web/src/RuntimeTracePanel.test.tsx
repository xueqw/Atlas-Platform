import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { RuntimeTraceContent } from './RuntimeTracePanel'
import {
  RUNTIME_EVENT_SCHEMA,
  createRuntimeRunProjection,
  runtimeRunReducer,
  type RuntimeEvent,
} from './runtime-events'
import type { RuntimeRunHandle } from './types'

const run: RuntimeRunHandle = {
  run_id: 'run-a',
  thread_id: 'workspace-a:run-a',
  status: 'running',
  execution_mode: 'langgraph',
  execution_strategy: 'react',
  created_at: '2026-07-16T00:00:00Z',
}

function runtimeEvent(sequence: number, type: RuntimeEvent['type'], payload: RuntimeEvent['payload']): RuntimeEvent {
  return {
    schema_version: RUNTIME_EVENT_SCHEMA,
    event_id: `event-${sequence}`,
    run_id: run.run_id,
    workspace_id: 'workspace-a',
    sequence,
    timestamp: '2026-07-16T00:00:00Z',
    type,
    payload,
  }
}

describe('RuntimeTraceContent', () => {
  it('renders failure, disconnected stream, retry, and safe fallback evidence', () => {
    let projection = createRuntimeRunProjection(run.run_id)
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(1, 'retry.scheduled', { target: 'knowledge_search', attempt: 2, delay_seconds: 1 }),
    })
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(2, 'runtime.fallback', { reason: 'checkpoint unavailable' }),
    })
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(3, 'run.failed', { message: 'legacy provider failed' }),
    })
    projection = runtimeRunReducer(projection, {
      type: 'connection',
      connection: { status: 'disconnected', attempt: 5, error: 'Runtime stream disconnected' },
    })

    const html = renderToStaticMarkup(<RuntimeTraceContent run={run} projection={projection} />)

    expect(html).toContain('执行失败')
    expect(html).toContain('连接已中断')
    expect(html).toContain('checkpoint unavailable')
    expect(html).toContain('legacy provider failed')
    expect(html).toContain('Runtime stream disconnected')
    expect(html).toContain('knowledge_search 第 2 次重试')
  })

  it('renders selected Multi-Agent strategy and independent reviewer evidence', () => {
    let projection = createRuntimeRunProjection(run.run_id)
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(1, 'strategy.selected', { selected: 'multi-agent-plan-execute-review' }),
    })
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(2, 'plan.created', {
        plan_id: 'plan-1',
        plan: {
          goal: '并行完成调研、实现和质量审查',
          version: 1,
          acceptance_criteria: ['功能完整', '证据充分'],
          workers: [
            { worker_id: 'researcher-1', role: 'Researcher', objective: '调研现有架构', allowed_tools: ['search'] },
            { worker_id: 'builder-1', role: 'Builder', objective: '完成实现与测试', allowed_tools: ['patch', 'test'] },
          ],
          tasks: [
            { task_id: 'research', worker_id: 'researcher-1', objective: '输出架构调研', depends_on: [] },
            { task_id: 'build', worker_id: 'builder-1', objective: '实现并验证功能', depends_on: ['research'] },
          ],
        },
      }),
    })
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(3, 'worker.created', { worker_id: 'researcher-1', role: 'Researcher' }),
    })
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(4, 'worker.created', { worker_id: 'builder-1', role: 'Builder' }),
    })
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(5, 'worker.created', { worker_id: 'reviewer-1', role: 'reviewer' }),
    })
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(6, 'review.completed', { reviewer_id: 'reviewer-1', verdict: 'PASS' }),
    })

    const html = renderToStaticMarkup(<RuntimeTraceContent run={run} projection={projection} />)
    expect(html).toContain('Multi-Agent · Plan · Execute · Review')
    expect(html).toContain('多智能体协作中')
    expect(html).toContain('Subagents')
    expect(html).toContain('Researcher')
    expect(html).toContain('Builder')
    expect(html).toContain('reviewer-1')
    expect(html).toContain('PASS')
    expect(html).toContain('并行完成调研、实现和质量审查')
  })

  it('renders a visible ReAct loop instead of pretending to create subagents', () => {
    let projection = createRuntimeRunProjection(run.run_id)
    projection = runtimeRunReducer(projection, {
      type: 'event',
      event: runtimeEvent(1, 'strategy.selected', {
        selected: 'react', reason_codes: ['bounded_react_sufficient'],
      }),
    })

    const html = renderToStaticMarkup(<RuntimeTraceContent run={run} projection={projection} />)
    expect(html).toContain('单智能体执行中')
    expect(html).toContain('THINK')
    expect(html).toContain('ACT')
    expect(html).toContain('OBSERVE')
    expect(html).not.toContain('EXECUTION TEAM')
  })
})
