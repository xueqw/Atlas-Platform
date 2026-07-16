import { createRuntimeSseStream, useRuntimeRun } from './useRuntimeRun'
import { runtimeEventStreamUrl } from './api'
import type { RuntimeRunHandle } from './types'

const stream = createRuntimeSseStream(runtimeEventStreamUrl)

const statusLabel: Record<string, string> = {
  pending: '等待调度',
  running: '正在执行',
  paused: '等待确认',
  succeeded: '已完成',
  failed: '执行失败',
  cancelled: '已取消',
}

const nodeLabel: Record<string, string> = {
  load_package: '加载智能体版本',
  load_memory: '读取记忆',
  route_skills: '匹配能力',
  plan: '生成执行计划',
  direct_response: '生成回答',
  react: '推理与工具选择',
  dispatch_tools: '调用只读工具',
  validate: '校验结果',
  finalize: '完成运行',
}

export default function RuntimeTracePanel({ run }: { run: RuntimeRunHandle }) {
  const projection = useRuntimeRun({ runId: run.run_id, stream })
  if (!projection) return null
  const nodes = Object.entries(projection.nodes)
  const retries = Object.entries(projection.retries)
  const errors = projection.errors
  const status = projection.status === 'pending' ? run.status : projection.status

  return <section className={`runtime-trace runtime-${status}`} aria-live="polite">
    <div className="runtime-trace-head">
      <div>
        <span>RUNTIME TRACE</span>
        <strong>执行轨迹</strong>
      </div>
      <div className="runtime-trace-state">
        <i />{statusLabel[status] || status}
      </div>
    </div>
    <div className="runtime-trace-meta">
      <span>{run.execution_mode === 'langgraph' ? 'LangGraph' : '兼容执行器'}</span>
      <span>事件 {projection.lastSequence}</span>
      <span>{projection.connection.status === 'terminated' ? '事件已回放' : '正在同步'}</span>
    </div>
    {nodes.length > 0 && <ol className="runtime-trace-nodes">
      {nodes.map(([name, state]) => <li key={name} className={`runtime-node runtime-node-${state.status}`}>
        <i />
        <span>{nodeLabel[name] || name}</span>
        <small>{state.status === 'running' ? '进行中' : state.status === 'succeeded' ? '完成' : state.status}</small>
      </li>)}
    </ol>}
    {(retries.length > 0 || errors.length > 0) && <details className="runtime-trace-detail">
      <summary>{errors.length > 0 ? '查看失败详情' : '查看重试记录'}</summary>
      {retries.map(([target, retry]) => <p key={target}>{target} 第 {retry.attempt} 次重试{retry.delaySeconds !== undefined ? `，等待 ${retry.delaySeconds}s` : ''}</p>)}
      {errors.map((error, index) => <p key={`${error.message || 'error'}-${index}`}>{String(error.message || error.category || '运行错误')}</p>)}
    </details>}
  </section>
}
