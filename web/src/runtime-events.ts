export const RUNTIME_EVENT_SCHEMA = 'atlas.runtime-event.v1' as const

export type RuntimeStatus = 'pending' | 'running' | 'paused' | 'succeeded' | 'failed' | 'cancelled'
export type RuntimeConnectionStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected' | 'terminated'
export type JsonPrimitive = string | number | boolean | null
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }
export type RuntimeEventPayload = { [key: string]: JsonValue }

export type KnownRuntimeEventType =
  | 'run.started'
  | 'run.resumed'
  | 'run.completed'
  | 'run.failed'
  | 'run.cancelled'
  | 'node.started'
  | 'node.completed'
  | 'node.failed'
  | 'token.delta'
  | 'response.token'
  | 'tool.started'
  | 'tool.completed'
  | 'tool.failed'
  | 'retry.scheduled'
  | 'interrupt.requested'
  | 'runtime.fallback'

export type RuntimeEventType = KnownRuntimeEventType | (string & {})

export type RuntimeEvent = Readonly<{
  schema_version: typeof RUNTIME_EVENT_SCHEMA
  event_id: string
  run_id: string
  workspace_id: string
  sequence: number
  timestamp: string
  type: RuntimeEventType
  payload: RuntimeEventPayload
}>

export type RuntimeNodeState = Readonly<{
  status: string
  updatedSequence: number
  error?: string
}>

export type RuntimeToolState = Readonly<{
  name: string
  status: string
  updatedSequence: number
  result?: JsonValue
  error?: string
}>

export type RuntimeRetryState = Readonly<{
  attempt: number
  delaySeconds?: number
  error?: string
  sequence: number
}>

export type RuntimeConnectionState = Readonly<{
  status: RuntimeConnectionStatus
  attempt: number
  error?: string
}>

export type RuntimeRunProjection = Readonly<{
  runId: string
  status: RuntimeStatus
  lastSequence: number
  seenEventIds: Readonly<Record<string, true>>
  connection: RuntimeConnectionState
  plan: RuntimeEventPayload | null
  nodes: Readonly<Record<string, RuntimeNodeState>>
  tokens: string
  sources: readonly JsonValue[]
  tools: Readonly<Record<string, RuntimeToolState>>
  retries: Readonly<Record<string, RuntimeRetryState>>
  errors: readonly RuntimeEventPayload[]
  pendingInterrupt: RuntimeEventPayload | null
}>

export type RuntimeRunAction =
  | { type: 'event'; event: RuntimeEvent }
  | { type: 'connection'; connection: RuntimeConnectionState }
  | { type: 'reset'; runId: string; cursor?: number }

const terminalStatuses = new Set<RuntimeStatus>(['succeeded', 'failed', 'cancelled'])

export function createRuntimeRunProjection(runId: string, cursor = 0): RuntimeRunProjection {
  if (!runId) throw new Error('runId is required')
  if (!Number.isInteger(cursor) || cursor < 0) throw new Error('cursor must be a non-negative integer')
  return {
    runId,
    status: 'pending',
    lastSequence: cursor,
    seenEventIds: {},
    connection: { status: 'idle', attempt: 0 },
    plan: null,
    nodes: {},
    tokens: '',
    sources: [],
    tools: {},
    retries: {},
    errors: [],
    pendingInterrupt: null,
  }
}

export function runtimeEventDisposition(
  state: RuntimeRunProjection,
  event: RuntimeEvent,
): 'accepted' | 'duplicate' | 'stale' | 'out_of_order' | 'wrong_run' {
  if (event.run_id !== state.runId) return 'wrong_run'
  if (event.schema_version !== RUNTIME_EVENT_SCHEMA || !Number.isInteger(event.sequence) || event.sequence < 1) return 'stale'
  if (state.seenEventIds[event.event_id]) return 'duplicate'
  if (event.sequence <= state.lastSequence) return 'stale'
  return event.sequence === state.lastSequence + 1 ? 'accepted' : 'out_of_order'
}

export function runtimeRunReducer(state: RuntimeRunProjection, action: RuntimeRunAction): RuntimeRunProjection {
  if (action.type === 'reset') return createRuntimeRunProjection(action.runId, action.cursor)
  if (action.type === 'connection') return { ...state, connection: action.connection }
  if (runtimeEventDisposition(state, action.event) !== 'accepted') return state

  const event = action.event
  let next: RuntimeRunProjection = {
    ...state,
    lastSequence: event.sequence,
    seenEventIds: { ...state.seenEventIds, [event.event_id]: true },
  }

  switch (event.type) {
    case 'run.started':
    case 'run.resumed':
      next = { ...next, status: 'running' }
      break
    case 'run.completed':
      next = { ...next, status: statusFrom(event.payload.status, 'succeeded'), pendingInterrupt: null }
      break
    case 'run.failed':
      next = { ...next, status: 'failed', errors: [...next.errors, event.payload], pendingInterrupt: null }
      break
    case 'run.cancelled':
      next = { ...next, status: 'cancelled', pendingInterrupt: null }
      break
    case 'node.started':
      next = withNode(next, event, 'running')
      break
    case 'node.completed':
      next = withNode(next, event, stringFrom(event.payload.status) || 'succeeded')
      break
    case 'node.failed':
      next = withNode(next, event, 'failed')
      break
    case 'token.delta':
    case 'response.token':
      next = { ...next, tokens: next.tokens + tokenFrom(event.payload) }
      break
    case 'plan.created':
    case 'plan.updated':
      next = { ...next, plan: event.payload }
      break
    case 'tool.started':
      next = withTool(next, event, 'running')
      break
    case 'tool.completed':
      next = withTool(next, event, 'succeeded')
      break
    case 'tool.failed':
      next = withTool(next, event, 'failed')
      break
    case 'retry.scheduled':
      next = withRetry(next, event)
      break
    case 'interrupt.requested':
      next = { ...next, status: 'paused', pendingInterrupt: event.payload }
      break
    case 'source.added':
      next = { ...next, sources: [...next.sources, event.payload] }
      break
    case 'sources.updated':
      next = { ...next, sources: arrayFrom(event.payload.sources) }
      break
  }

  return next
}

export function isTerminalRuntimeStatus(status: RuntimeStatus): boolean {
  return terminalStatuses.has(status)
}

function withNode(state: RuntimeRunProjection, event: RuntimeEvent, fallbackStatus: string): RuntimeRunProjection {
  const name = stringFrom(event.payload.node) || stringFrom(event.payload.name) || 'unknown'
  const error = stringFrom(event.payload.error) || stringFrom(event.payload.message)
  return {
    ...state,
    nodes: { ...state.nodes, [name]: { status: fallbackStatus, updatedSequence: event.sequence, ...(error ? { error } : {}) } },
  }
}

function withTool(state: RuntimeRunProjection, event: RuntimeEvent, status: string): RuntimeRunProjection {
  const name = stringFrom(event.payload.name) || stringFrom(event.payload.tool) || 'unknown'
  const error = stringFrom(event.payload.error) || stringFrom(event.payload.message)
  return {
    ...state,
    tools: {
      ...state.tools,
      [name]: { name, status, updatedSequence: event.sequence, ...(event.payload.result !== undefined ? { result: event.payload.result } : {}), ...(error ? { error } : {}) },
    },
  }
}

function withRetry(state: RuntimeRunProjection, event: RuntimeEvent): RuntimeRunProjection {
  const target = stringFrom(event.payload.target) || 'runtime'
  const attempt = numberFrom(event.payload.attempt) || 0
  const delaySeconds = numberFrom(event.payload.delay_seconds)
  const error = stringFrom(event.payload.error)
  return {
    ...state,
    retries: { ...state.retries, [target]: { attempt, sequence: event.sequence, ...(delaySeconds !== undefined ? { delaySeconds } : {}), ...(error ? { error } : {}) } },
  }
}

function statusFrom(value: JsonValue | undefined, fallback: RuntimeStatus): RuntimeStatus {
  return typeof value === 'string' && ['pending', 'running', 'paused', 'succeeded', 'failed', 'cancelled'].includes(value)
    ? value as RuntimeStatus
    : fallback
}

function stringFrom(value: JsonValue | undefined): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function numberFrom(value: JsonValue | undefined): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function arrayFrom(value: JsonValue | undefined): readonly JsonValue[] {
  return Array.isArray(value) ? value : []
}

function tokenFrom(payload: RuntimeEventPayload): string {
  return stringFrom(payload.content) || stringFrom(payload.token) || stringFrom(payload.text) || ''
}
