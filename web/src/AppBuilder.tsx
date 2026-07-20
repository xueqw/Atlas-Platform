import { useEffect, useRef, useState } from 'react'
import {
  APPLICATION_RUNTIME_BRIDGE_SCHEMA,
  bridgeEvent,
  isApplicationRuntimeBridgeCommand,
  isApplicationRuntimeBridgeEvent,
} from './application-runtime-bridge'
import { RUNTIME_EVENT_SCHEMA } from './runtime-events'
import { createRuntimeSseStream } from './useRuntimeRun'
import type { Agent, KnowledgeBase } from './types'
import './webide.css'

const gatewayOrigin = (import.meta.env.VITE_AGENTGATEWAY_URL || 'http://localhost:3100').replace(/\/$/, '')

type Props = {
  agents: Agent[]
  knowledge: KnowledgeBase[]
  refresh: () => Promise<void> | void
  notice: (text: string) => void
  onInvokeAgent: (agent: Agent) => void
}

export default function AppBuilder(_props: Props) {
  const { notice, agents } = _props
  const frameRef = useRef<HTMLIFrameElement>(null)
  const streamControllersRef = useRef(new Map<string, AbortController>())
  const [bridgeReady, setBridgeReady] = useState(false)
  const [runtimeAgentId, setRuntimeAgentId] = useState('')

  useEffect(() => {
    const expectedOrigin = new URL(gatewayOrigin).origin
    const post = (message: unknown) => frameRef.current?.contentWindow?.postMessage(message, expectedOrigin)
    const runtimeStream = createRuntimeSseStream()

    const requestJson = async (path: string, init: RequestInit) => {
      const response = await fetch(path, {
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...init.headers },
        ...init,
      })
      let body: Record<string, unknown> = {}
      try {
        body = await response.json() as Record<string, unknown>
      } catch {
        // The status code remains authoritative when an intermediary has no JSON body.
      }
      if (!response.ok) {
        const detail = typeof body.detail === 'string' ? body.detail : `Runtime 请求失败 (${response.status})`
        throw new Error(detail)
      }
      return body
    }

    const executeCommand = async (command: Parameters<typeof isApplicationRuntimeBridgeCommand>[0]) => {
      if (!isApplicationRuntimeBridgeCommand(command)) return
      const requestId = command.request_id
      const payload = command.payload as Record<string, unknown>
      if (command.type === 'runtime.subscribe' && payload.handshake === true) {
        setBridgeReady(true)
        post(bridgeEvent('runtime.ready', requestId, { supported_event_schema: RUNTIME_EVENT_SCHEMA }))
        return
      }

      try {
        if (command.type === 'runtime.start') {
          const result = await requestJson('/api/runtime/runs', { method: 'POST', body: JSON.stringify(payload) })
          post(bridgeEvent('runtime.ack', requestId, {
            run_id: String(result.run_id || ''), status: String(result.status || 'pending'), cursor: 0,
          }))
          return
        }
        const runId = String(payload.run_id)
        if (command.type === 'runtime.resume') {
          const { run_id: _runId, ...resumePayload } = payload
          const result = await requestJson(`/api/runtime/runs/${encodeURIComponent(runId)}/resume`, {
            method: 'POST', body: JSON.stringify(resumePayload),
          })
          post(bridgeEvent('runtime.ack', requestId, {
            run_id: runId, status: String(result.status || 'running'), cursor: Number(result.last_sequence || 0),
          }))
          return
        }
        if (command.type === 'runtime.cancel') {
          const result = await requestJson(`/api/runtime/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' })
          post(bridgeEvent('runtime.ack', requestId, {
            run_id: runId, status: String(result.status || 'cancelled'), cursor: Number(result.last_sequence || 0),
          }))
          return
        }

        const previous = streamControllersRef.current.get(requestId)
        previous?.abort()
        const controller = new AbortController()
        streamControllersRef.current.set(requestId, controller)
        let cursor = Number(payload.after_sequence || 0)
        post(bridgeEvent('runtime.ack', requestId, { run_id: runId, status: 'streaming', cursor }))
        await runtimeStream({
          runId,
          cursor,
          signal: controller.signal,
          onEvent: (event) => {
            cursor = event.sequence
            post(bridgeEvent('runtime.event', requestId, { event, run_id: runId, cursor }))
          },
        })
        if (!controller.signal.aborted) {
          post(bridgeEvent('runtime.ack', requestId, { run_id: runId, status: 'stream_closed', cursor }))
        }
        streamControllersRef.current.delete(requestId)
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        const message = error instanceof Error ? error.message : 'Runtime bridge 请求失败'
        notice(message)
        post(bridgeEvent('runtime.error', requestId, { message }))
      }
    }

    const receive = (event: MessageEvent<unknown>) => {
      if (event.origin !== expectedOrigin || event.source !== frameRef.current?.contentWindow) return
      if (isApplicationRuntimeBridgeCommand(event.data)) {
        void executeCommand(event.data)
        return
      }
      if (isApplicationRuntimeBridgeEvent(event.data) && event.data.type === 'runtime.ready') setBridgeReady(true)
    }
    window.addEventListener('message', receive)
    return () => {
      window.removeEventListener('message', receive)
      for (const controller of streamControllersRef.current.values()) controller.abort()
      streamControllersRef.current.clear()
    }
  }, [notice])

  const announceHost = () => {
    frameRef.current?.contentWindow?.postMessage({
      schema_version: APPLICATION_RUNTIME_BRIDGE_SCHEMA,
      type: 'runtime.subscribe',
      request_id: crypto.randomUUID(),
      payload: { handshake: true, event_schema: RUNTIME_EVENT_SCHEMA },
    }, new URL(gatewayOrigin).origin)
  }

  const gatewayWorkbenchUrl = runtimeAgentId
    ? `${gatewayOrigin}/workbench?atlas_agent_id=${encodeURIComponent(runtimeAgentId)}`
    : `${gatewayOrigin}/workbench`

  return (
    <section className="gateway-builder">
      <header className="gateway-builder-header">
        <div>
          <span>APPLICATION DEVELOPMENT</span>
          <h1>智能体开发工作台</h1>
          <p>规划、DAG 编排、测试、评测、监控和发布统一在一个开发模块中完成。</p>
          <small>{bridgeReady ? 'Runtime Bridge 已连接' : 'Runtime Bridge 等待连接'}</small>
          <label>
            Atlas Runtime 智能体映射
            <select value={runtimeAgentId} onChange={(event) => { setBridgeReady(false); setRuntimeAgentId(event.target.value) }}>
              <option value="">请选择智能体</option>
              {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
            </select>
          </label>
        </div>
        <a href={gatewayWorkbenchUrl} target="_blank" rel="noreferrer">在新窗口打开</a>
      </header>
      <iframe
        ref={frameRef}
        className="gateway-builder-frame"
        src={gatewayWorkbenchUrl}
        title="AgentGateway 智能体开发工作台"
        allow="clipboard-read; clipboard-write"
        onLoad={announceHost}
      />
    </section>
  )
}
