import {
  RUNTIME_EVENT_SCHEMA,
  createRuntimeRunProjection,
  runtimeEventDisposition,
  runtimeRunReducer,
  selectRuntimeTransport,
  type RuntimeEvent,
} from './runtime-events'
import { createRuntimeSseStream, runtimeReconnectDelayMs } from './useRuntimeRun'
import { describe, expect, it } from 'vitest'

const event = (overrides: Partial<RuntimeEvent> = {}): RuntimeEvent => ({
  schema_version: RUNTIME_EVENT_SCHEMA,
  event_id: 'event-1',
  run_id: 'run-a',
  workspace_id: 'workspace-a',
  sequence: 1,
  timestamp: '2026-07-15T00:00:00.000Z',
  type: 'token.delta',
  payload: { content: 'hello' },
  ...overrides,
})

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

/** Pure cases that a future Vitest/Jest setup can invoke without DOM test helpers. */
export function runRuntimeEventReducerTests(): void {
  let state = createRuntimeRunProjection('run-a')
  state = runtimeRunReducer(state, { type: 'event', event: event() })
  assert(state.tokens === 'hello' && state.lastSequence === 1, 'applies the next event once')

  state = runtimeRunReducer(state, { type: 'event', event: event() })
  assert(state.tokens === 'hello', 'de-duplicates stable event ids')

  const gap = event({ event_id: 'event-3', sequence: 3, payload: { content: 'ignored' } })
  assert(runtimeEventDisposition(state, gap) === 'out_of_order', 'rejects sequence gaps')
  state = runtimeRunReducer(state, { type: 'event', event: gap })
  assert(state.lastSequence === 1, 'does not advance the cursor across a sequence gap')

  const otherRun = event({ event_id: 'event-b', run_id: 'run-b', sequence: 2 })
  assert(runtimeEventDisposition(state, otherRun) === 'wrong_run', 'keeps projections run-scoped')

  const second = event({ event_id: 'event-2', sequence: 2, type: 'node.completed', payload: { node: 'plan', status: 'succeeded' } })
  state = runtimeRunReducer(state, { type: 'event', event: second })
  assert(state.nodes.plan?.status === 'succeeded', 'projects node state')

  assert(runtimeReconnectDelayMs(1, { jitterRatio: 0 }, () => 0.5) === 250, 'uses initial reconnect delay')
  assert(runtimeReconnectDelayMs(5, { jitterRatio: 0, maxDelayMs: 1_000 }, () => 0.5) === 1_000, 'caps reconnect delay')
}

describe('runtime event projection', () => {
  it('deduplicates, preserves cursor order, and computes reconnect backoff', () => {
    runRuntimeEventReducerTests()
  })

  it('resumes the SSE request from its cursor and parses only versioned events', async () => {
    const received: RuntimeEvent[] = []
    let requestedUrl = ''
    const resumed = event({ event_id: 'event-5', sequence: 5, payload: { content: 'resumed' } })
    const fetchImpl = (async (input: RequestInfo | URL) => {
      requestedUrl = String(input)
      return new Response(`id: 5\nevent: token.delta\ndata: ${JSON.stringify(resumed)}\n\n`, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      })
    }) as typeof fetch

    await createRuntimeSseStream(undefined, fetchImpl)({
      runId: 'run-a',
      cursor: 4,
      signal: new AbortController().signal,
      onEvent: (item) => received.push(item),
    })

    expect(requestedUrl).toContain('after_sequence=4')
    expect(received).toEqual([resumed])
  })

  it('projects terminal failures and explicit safe fallback without losing the cursor', () => {
    let state = createRuntimeRunProjection('run-a')
    state = runtimeRunReducer(state, { type: 'event', event: event({
      type: 'runtime.fallback', payload: { reason: 'checkpoint unavailable' },
    }) })
    state = runtimeRunReducer(state, { type: 'event', event: event({
      event_id: 'event-2', sequence: 2, type: 'run.failed', payload: { message: 'legacy failed' },
    }) })

    expect(state.fallback?.reason).toBe('checkpoint unavailable')
    expect(state.status).toBe('failed')
    expect(state.errors[0]?.message).toBe('legacy failed')
    expect(state.lastSequence).toBe(2)
  })

  it('selects the legacy feature fallback whenever the flag or request eligibility is false', () => {
    expect(selectRuntimeTransport(true, true)).toBe('langgraph')
    expect(selectRuntimeTransport(false, true)).toBe('legacy')
    expect(selectRuntimeTransport(true, false)).toBe('legacy')
  })

  it('projects adaptive strategy, isolated workers, independent review, and escalation', () => {
    let state = createRuntimeRunProjection('run-a')
    const events: RuntimeEvent[] = [
      event({ type: 'strategy.selected', payload: { selected: 'multi-agent-plan-execute-review' } }),
      event({ event_id: 'event-2', sequence: 2, type: 'worker.created', payload: { worker_id: 'reviewer-1', role: 'reviewer' } }),
      event({ event_id: 'event-3', sequence: 3, type: 'review.completed', payload: { reviewer_id: 'reviewer-1', verdict: 'REPLAN' } }),
      event({ event_id: 'event-4', sequence: 4, type: 'run.escalated', payload: { reason: 'human decision' } }),
    ]
    for (const item of events) state = runtimeRunReducer(state, { type: 'event', event: item })

    expect(state.strategy?.selected).toBe('multi-agent-plan-execute-review')
    expect(state.workers['reviewer-1']?.role).toBe('reviewer')
    expect(state.reviews[0]?.verdict).toBe('REPLAN')
    expect(state.status).toBe('paused')
    expect(state.escalation?.reason).toBe('human decision')
  })

  it('shows the persisted side-effect boundary and clears an approved write interrupt', () => {
    let state = createRuntimeRunProjection('run-a')
    state = runtimeRunReducer(state, { type: 'event', event: event({
      type: 'side_effect.boundary_started', payload: { task_ids: ['write-task'] },
    }) })
    state = runtimeRunReducer(state, { type: 'event', event: event({
      event_id: 'event-2', sequence: 2, type: 'interrupt.requested',
      payload: { reason: 'worker_authorization_required', tool_name: 'send' },
    }) })
    expect(state.sideEffectBoundary?.task_ids).toEqual(['write-task'])
    expect(state.pendingInterrupt?.tool_name).toBe('send')
    expect(state.status).toBe('paused')

    state = runtimeRunReducer(state, { type: 'event', event: event({
      event_id: 'event-3', sequence: 3, type: 'interrupt.resolved',
      payload: { decision: 'approved' },
    }) })
    expect(state.pendingInterrupt).toBeNull()
    expect(state.status).toBe('running')
  })
})
