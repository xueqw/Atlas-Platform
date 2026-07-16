import { useEffect, useRef, useState } from 'react'
import {
  RUNTIME_EVENT_SCHEMA,
  createRuntimeRunProjection,
  isTerminalRuntimeStatus,
  runtimeEventDisposition,
  runtimeRunReducer,
  type RuntimeConnectionState,
  type RuntimeEvent,
  type RuntimeRunProjection,
} from './runtime-events'

export type RuntimeEventStream = (options: {
  runId: string
  cursor: number
  signal: AbortSignal
  onEvent: (event: RuntimeEvent) => void
}) => Promise<void>

export type RuntimeReconnectOptions = Readonly<{
  initialDelayMs: number
  maxDelayMs: number
  maxAttempts: number
  jitterRatio: number
}>

export const defaultRuntimeReconnectOptions: RuntimeReconnectOptions = {
  initialDelayMs: 250,
  maxDelayMs: 4_000,
  maxAttempts: 5,
  jitterRatio: 0.2,
}

export function runtimeReconnectDelayMs(
  attempt: number,
  options: Partial<RuntimeReconnectOptions> = {},
  random = Math.random,
): number {
  const config = { ...defaultRuntimeReconnectOptions, ...options }
  if (!Number.isInteger(attempt) || attempt < 1) return 0
  const capped = Math.min(config.maxDelayMs, config.initialDelayMs * (2 ** (attempt - 1)))
  const jitter = (random() * 2 - 1) * config.jitterRatio
  return Math.max(0, Math.round(capped * (1 + jitter)))
}

export function runtimeEventStreamUrl(runId: string, cursor: number): string {
  return `/api/runtime/runs/${encodeURIComponent(runId)}/events/stream?after_sequence=${cursor}`
}

export function createRuntimeSseStream(
  urlForRun: (runId: string, cursor: number) => string = runtimeEventStreamUrl,
  fetchImpl: typeof fetch = fetch,
): RuntimeEventStream {
  return async ({ runId, cursor, signal, onEvent }) => {
    const response = await fetchImpl(urlForRun(runId, cursor), {
      credentials: 'include',
      headers: { Accept: 'text/event-stream' },
      signal,
    })
    if (!response.ok) throw new Error(`Runtime stream request failed (${response.status})`)
    if (!response.body) throw new Error('Runtime stream response has no body')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) return
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split(/\r?\n\r?\n/)
      buffer = blocks.pop() || ''
      for (const block of blocks) {
        const data = block.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
        if (!data) continue
        const event = JSON.parse(data) as RuntimeEvent
        if (event.schema_version !== RUNTIME_EVENT_SCHEMA) throw new Error('Unsupported runtime event schema')
        onEvent(event)
      }
    }
  }
}

export type UseRuntimeRunOptions = Readonly<{
  runId: string | null | undefined
  stream: RuntimeEventStream
  enabled?: boolean
  cursor?: number
  reconnect?: Partial<RuntimeReconnectOptions>
}>

export function useRuntimeRun({ runId, stream, enabled = true, cursor = 0, reconnect }: UseRuntimeRunOptions): RuntimeRunProjection | null {
  const [projection, setProjection] = useState<RuntimeRunProjection | null>(null)
  const projectionRef = useRef<RuntimeRunProjection | null>(null)

  useEffect(() => {
    if (!runId || !enabled) {
      projectionRef.current = null
      setProjection(null)
      return
    }

    const initial = createRuntimeRunProjection(runId, cursor)
    const config = { ...defaultRuntimeReconnectOptions, ...reconnect }
    const controller = new AbortController()
    let retryTimer: number | undefined
    let attempt = 0
    projectionRef.current = initial
    setProjection(initial)

    const update = (action: Parameters<typeof runtimeRunReducer>[1]) => {
      const current = projectionRef.current
      if (!current) return current
      const next = runtimeRunReducer(current, action)
      projectionRef.current = next
      setProjection(next)
      return next
    }

    const connection = (status: RuntimeConnectionState['status'], error?: string) =>
      update({ type: 'connection', connection: { status, attempt, ...(error ? { error } : {}) } })

    const connect = () => {
      if (controller.signal.aborted || !projectionRef.current) return
      connection(attempt === 0 ? 'connecting' : 'reconnecting')
      void stream({
        runId,
        cursor: projectionRef.current.lastSequence,
        signal: controller.signal,
        onEvent: (event) => {
          const current = projectionRef.current
          if (!current || event.run_id !== runId) return
          const disposition = runtimeEventDisposition(current, event)
          if (disposition === 'out_of_order') throw new Error('Runtime event sequence gap')
          if (disposition === 'accepted') update({ type: 'event', event })
        },
      }).then(() => finishOrReconnect()).catch((error: unknown) => finishOrReconnect(error))
    }

    const finishOrReconnect = (error?: unknown) => {
      const current = projectionRef.current
      if (controller.signal.aborted || !current) return
      if (isTerminalRuntimeStatus(current.status)) {
        connection('terminated')
        return
      }
      attempt += 1
      if (attempt > config.maxAttempts) {
        connection('disconnected', errorMessage(error) || 'Runtime stream disconnected')
        return
      }
      connection('reconnecting', errorMessage(error))
      retryTimer = window.setTimeout(connect, runtimeReconnectDelayMs(attempt, config))
    }

    connect()
    return () => {
      controller.abort()
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [runId, stream, enabled, cursor, reconnect])

  return projection
}

function errorMessage(error: unknown): string | undefined {
  return error instanceof Error ? error.message : typeof error === 'string' ? error : undefined
}
