import {
  APPLICATION_RUNTIME_BRIDGE_SCHEMA,
  isApplicationRuntimeBridgeCommand,
  isApplicationRuntimeBridgeEvent,
} from './application-runtime-bridge'
import { RUNTIME_EVENT_SCHEMA } from './runtime-events'
import { describe, it } from 'vitest'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

export function runApplicationRuntimeBridgeTests(): void {
  const valid = {
    schema_version: APPLICATION_RUNTIME_BRIDGE_SCHEMA,
    type: 'runtime.event',
    request_id: 'request-1',
    payload: {
      event: {
        schema_version: RUNTIME_EVENT_SCHEMA,
        event_id: 'event-1',
        run_id: 'run-1',
        workspace_id: 'workspace-1',
        sequence: 1,
        timestamp: '2026-07-16T00:00:00Z',
        type: 'run.started',
        payload: {},
      },
    },
  }
  assert(isApplicationRuntimeBridgeEvent(valid), 'accepts the versioned Atlas Runtime Event envelope')
  assert(!isApplicationRuntimeBridgeEvent({ ...valid, schema_version: 'unknown' }), 'rejects bridge schema drift')
  assert(!isApplicationRuntimeBridgeEvent({
    ...valid,
    payload: { event: { ...valid.payload.event, schema_version: 'langgraph.chunk.v1' } },
  }), 'rejects LangGraph-internal event payloads')

  const start = {
    schema_version: APPLICATION_RUNTIME_BRIDGE_SCHEMA,
    type: 'runtime.start',
    request_id: 'request-start',
    payload: { agent_id: 'agent-1', input: 'hello', idempotency_key: 'idempotency-1', source: 'builder' },
  }
  assert(isApplicationRuntimeBridgeCommand(start), 'accepts a fully validated start command')
  assert(!isApplicationRuntimeBridgeCommand({ ...start, payload: { ...start.payload, workspace_id: 'forged' } }), 'rejects identity fields outside the command schema')
  assert(isApplicationRuntimeBridgeCommand({
    ...start, type: 'runtime.subscribe', payload: { run_id: 'run-1', after_sequence: 4, event_schema: RUNTIME_EVENT_SCHEMA },
  }), 'accepts cursor-based subscriptions')
  assert(!isApplicationRuntimeBridgeCommand({
    ...start, type: 'runtime.subscribe', payload: { run_id: 'run-1', after_sequence: -1 },
  }), 'rejects invalid subscription cursors')

  const resume = {
    ...start,
    type: 'runtime.resume',
    payload: {
      run_id: 'run-1',
      interrupt_id: 'interrupt-1',
      nonce: 'nonce-1',
      decision: 'approve',
      parameter_digest: 'sha256:parameters',
      resource_version: 'tool:v1',
    },
  }
  assert(isApplicationRuntimeBridgeCommand(resume), 'accepts an atomically bound interrupt resume')
  assert(isApplicationRuntimeBridgeCommand({
    ...resume, payload: { run_id: 'run-1' },
  }), 'accepts an unbound paused-run resume')
  assert(!isApplicationRuntimeBridgeCommand({
    ...resume, payload: { run_id: 'run-1', interrupt_id: 'interrupt-1', decision: 'approve' },
  }), 'rejects a partially bound interrupt resume')
  assert(!isApplicationRuntimeBridgeCommand({
    ...resume, payload: { ...resume.payload, nonce: '' },
  }), 'rejects an interrupt resume with an empty binding member')
}

describe('application runtime bridge', () => {
  it('accepts only the versioned bridge and Atlas Runtime Event schema', () => {
    runApplicationRuntimeBridgeTests()
  })
})
