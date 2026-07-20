import { RUNTIME_EVENT_SCHEMA, type RuntimeEvent } from './runtime-events'

export const APPLICATION_RUNTIME_BRIDGE_SCHEMA = 'atlas.application-runtime-bridge.v1' as const

export type RuntimeStartBridgePayload = Readonly<{
  agent_id: string
  input: string
  idempotency_key: string
  source?: 'workbench' | 'builder' | 'subagent' | 'chat' | 'preview' | 'api' | 'evaluation'
  version_id?: string
  conversation_id?: string
  requested_resources?: readonly string[]
}>

type RuntimeResumeWithoutInterrupt = Readonly<{
  interrupt_id?: never
  nonce?: never
  decision?: never
  parameter_digest?: never
  resource_version?: never
}>

type RuntimeResumeInterruptBinding = Readonly<{
  interrupt_id: string
  nonce: string
  decision: 'approve' | 'deny'
  parameter_digest: string
  resource_version: string
}>

export type RuntimeResumeBridgePayload = Readonly<{ run_id: string }>
  & (RuntimeResumeWithoutInterrupt | RuntimeResumeInterruptBinding)

export type RuntimeCancelBridgePayload = Readonly<{ run_id: string }>
export type RuntimeSubscribeBridgePayload = Readonly<{
  run_id?: string
  after_sequence?: number
  handshake?: boolean
  event_schema?: typeof RUNTIME_EVENT_SCHEMA
}>

export type ApplicationRuntimeBridgeCommand = Readonly<{
  schema_version: typeof APPLICATION_RUNTIME_BRIDGE_SCHEMA
  type: 'runtime.start' | 'runtime.cancel' | 'runtime.resume' | 'runtime.subscribe'
  request_id: string
  payload: RuntimeStartBridgePayload | RuntimeResumeBridgePayload | RuntimeCancelBridgePayload | RuntimeSubscribeBridgePayload
}>

export type ApplicationRuntimeBridgeEvent = Readonly<{
  schema_version: typeof APPLICATION_RUNTIME_BRIDGE_SCHEMA
  type: 'runtime.ready' | 'runtime.ack' | 'runtime.event' | 'runtime.error'
  request_id: string
  payload: {
    event?: RuntimeEvent
    message?: string
    supported_event_schema?: string
    run_id?: string
    status?: string
    cursor?: number
  }
}>

const COMMAND_TYPES = new Set<ApplicationRuntimeBridgeCommand['type']>([
  'runtime.start', 'runtime.cancel', 'runtime.resume', 'runtime.subscribe',
])
const RESPONSE_TYPES = new Set<ApplicationRuntimeBridgeEvent['type']>([
  'runtime.ready', 'runtime.ack', 'runtime.event', 'runtime.error',
])
const REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function hasRequestId(value: unknown): value is { request_id: string } {
  return isRecord(value) && typeof value.request_id === 'string' && REQUEST_ID_PATTERN.test(value.request_id)
}

function isNonEmptyString(value: unknown, maximum = 100_000): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum
}

function isOptionalString(value: unknown, maximum = 128): boolean {
  return value === undefined || isNonEmptyString(value, maximum)
}

function hasOnlyKeys(payload: Record<string, unknown>, allowed: readonly string[]): boolean {
  const allowedKeys = new Set(allowed)
  return Object.keys(payload).every((key) => allowedKeys.has(key))
}

function validCommandPayload(type: ApplicationRuntimeBridgeCommand['type'], payload: Record<string, unknown>): boolean {
  if (type === 'runtime.start') {
    if (!hasOnlyKeys(payload, ['agent_id', 'input', 'idempotency_key', 'source', 'version_id', 'conversation_id', 'requested_resources'])) return false
    if (!isNonEmptyString(payload.agent_id, 128) || !isNonEmptyString(payload.input) || !isNonEmptyString(payload.idempotency_key, 120)) return false
    if (payload.source !== undefined && !['workbench', 'builder', 'subagent', 'chat', 'preview', 'api', 'evaluation'].includes(String(payload.source))) return false
    if (!isOptionalString(payload.version_id) || !isOptionalString(payload.conversation_id)) return false
    if (payload.requested_resources !== undefined && (!Array.isArray(payload.requested_resources) || payload.requested_resources.some((item) => !isNonEmptyString(item, 256)))) return false
    return true
  }
  if (type === 'runtime.resume') {
    if (!hasOnlyKeys(payload, ['run_id', 'interrupt_id', 'nonce', 'decision', 'parameter_digest', 'resource_version'])) return false
    if (!isNonEmptyString(payload.run_id, 128)) return false
    const binding = [payload.interrupt_id, payload.nonce, payload.decision, payload.parameter_digest, payload.resource_version]
    const hasAnyBinding = binding.some((value) => value !== undefined)
    if (!hasAnyBinding) return true
    return isNonEmptyString(payload.interrupt_id, 128)
      && isNonEmptyString(payload.nonce, 128)
      && (payload.decision === 'approve' || payload.decision === 'deny')
      && isNonEmptyString(payload.parameter_digest, 128)
      && isNonEmptyString(payload.resource_version, 128)
  }
  if (type === 'runtime.cancel') {
    return hasOnlyKeys(payload, ['run_id']) && isNonEmptyString(payload.run_id, 128)
  }
  if (!hasOnlyKeys(payload, ['run_id', 'after_sequence', 'handshake', 'event_schema'])) return false
  if (payload.handshake === true) {
    return payload.run_id === undefined && payload.after_sequence === undefined
      && (payload.event_schema === undefined || payload.event_schema === RUNTIME_EVENT_SCHEMA)
  }
  return isNonEmptyString(payload.run_id, 128)
    && (payload.after_sequence === undefined || (Number.isInteger(payload.after_sequence) && Number(payload.after_sequence) >= 0))
    && (payload.event_schema === undefined || payload.event_schema === RUNTIME_EVENT_SCHEMA)
}

export function isApplicationRuntimeBridgeCommand(value: unknown): value is ApplicationRuntimeBridgeCommand {
  if (!isRecord(value) || !hasRequestId(value)) return false
  const candidate = value as Record<string, unknown> & { request_id: string }
  if (candidate.schema_version !== APPLICATION_RUNTIME_BRIDGE_SCHEMA) return false
  if (typeof candidate.type !== 'string' || !COMMAND_TYPES.has(candidate.type as ApplicationRuntimeBridgeCommand['type'])) return false
  return isRecord(candidate.payload) && validCommandPayload(candidate.type as ApplicationRuntimeBridgeCommand['type'], candidate.payload)
}

function isRuntimeEvent(value: unknown): value is RuntimeEvent {
  if (!isRecord(value)) return false
  return value.schema_version === RUNTIME_EVENT_SCHEMA
    && isNonEmptyString(value.event_id, 128)
    && isNonEmptyString(value.run_id, 128)
    && isNonEmptyString(value.workspace_id, 128)
    && Number.isInteger(value.sequence) && Number(value.sequence) >= 1
    && isNonEmptyString(value.timestamp, 100)
    && isNonEmptyString(value.type, 100)
    && isRecord(value.payload)
}

export function isApplicationRuntimeBridgeEvent(value: unknown): value is ApplicationRuntimeBridgeEvent {
  if (!isRecord(value) || !hasRequestId(value)) return false
  const candidate = value as Record<string, unknown> & { request_id: string }
  if (candidate.schema_version !== APPLICATION_RUNTIME_BRIDGE_SCHEMA) return false
  if (typeof candidate.type !== 'string' || !RESPONSE_TYPES.has(candidate.type as ApplicationRuntimeBridgeEvent['type'])) return false
  if (!isRecord(candidate.payload)) return false
  if (candidate.type === 'runtime.event') return isRuntimeEvent(candidate.payload.event)
  if (candidate.type === 'runtime.error') return isNonEmptyString(candidate.payload.message, 2_000)
  if (candidate.type === 'runtime.ready') return candidate.payload.supported_event_schema === RUNTIME_EVENT_SCHEMA
  return isNonEmptyString(candidate.payload.run_id, 128) && typeof candidate.payload.status === 'string'
}

export function bridgeEvent(
  type: ApplicationRuntimeBridgeEvent['type'],
  requestId: string,
  payload: ApplicationRuntimeBridgeEvent['payload'],
): ApplicationRuntimeBridgeEvent {
  return { schema_version: APPLICATION_RUNTIME_BRIDGE_SCHEMA, type, request_id: requestId, payload }
}
