import {
  RUNTIME_EVENT_SCHEMA,
  createRuntimeRunProjection,
  runtimeEventDisposition,
  runtimeRunReducer,
  type RuntimeEvent,
} from './runtime-events'
import { runtimeReconnectDelayMs } from './useRuntimeRun'

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
