import { useEffect } from 'react'
import { createRuntimeSseStream, useRuntimeRun } from './useRuntimeRun'
import type { JsonValue, RuntimeRunProjection, RuntimeWorkerState } from './runtime-events'
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
  created: '已创建',
  timed_out: '已超时',
}

const nodeLabel: Record<string, string> = {
  load_package: '加载智能体版本',
  load_memory: '读取记忆',
  route_skills: '匹配能力',
  plan: '拆解目标与生成 DAG',
  prepare_execute: '建立副作用安全边界',
  execute: '调度 Subagents',
  create_reviewer: '创建独立 Reviewer',
  review: '审查结果与证据',
  decide: '处理审查裁决',
  direct_response: '生成回答',
  react: 'ReAct 推理与行动',
  dispatch_tools: '调用工具',
  validate: '校验结果',
  finalize: '汇总并完成',
}

const connectionLabel: Record<string, string> = {
  idle: '等待连接',
  connecting: '正在连接',
  connected: '实时同步',
  reconnecting: '正在重连',
  disconnected: '连接已中断',
  terminated: '事件已回放',
}

const strategyLabel: Record<string, string> = {
  auto: '自动选择',
  react: 'ReAct',
  'plan-execute-review': 'Plan · Execute · Review',
  'multi-agent-plan-execute-review': 'Multi-Agent · Plan · Execute · Review',
}

const reasonLabel: Record<string, string> = {
  multiple_objectives: '多个目标',
  multi_step: '多步骤任务',
  task_dependencies: '存在任务依赖',
  reviewed_artifact: '需要审查交付物',
  write_or_side_effect: '包含写操作',
  high_risk: '高风险任务',
  parallel_workstreams: '可并行执行',
  specialist_roles: '需要专业角色',
  high_ambiguity: '需求存在歧义',
  large_request: '大型任务',
  multi_agent_execution_required: '需要多智能体',
  reviewed_plan_required: '需要独立审查',
  bounded_react_sufficient: 'ReAct 足以完成',
}

type PlanWorker = Readonly<{
  workerId: string
  role: string
  objective: string
  allowedTools: readonly string[]
}>

type PlanTask = Readonly<{
  taskId: string
  workerId: string
  objective: string
  dependsOn: readonly string[]
}>

type RuntimePlan = Readonly<{
  goal: string
  version?: number
  workers: readonly PlanWorker[]
  tasks: readonly PlanTask[]
  acceptanceCriteria: readonly string[]
}>

export default function RuntimeTracePanel({ run, onProjection }: {
  run: RuntimeRunHandle
  onProjection?: (projection: RuntimeRunProjection) => void
}) {
  const projection = useRuntimeRun({ runId: run.run_id, stream })
  useEffect(() => {
    if (projection) onProjection?.(projection)
  }, [projection, onProjection])
  if (!projection) return null
  return <RuntimeTraceContent run={run} projection={projection} />
}

export function RuntimeTraceContent({ run, projection }: {
  run: RuntimeRunHandle
  projection: RuntimeRunProjection
}) {
  const nodes = Object.entries(projection.nodes)
  const retries = Object.entries(projection.retries)
  const errors = projection.errors
  const plan = runtimePlanFrom(projection.plan?.plan)
  const projectedWorkers = projection.workers
  const reviewer = Object.values(projectedWorkers).find((worker) => worker.role === 'reviewer')
  const planWorkers = plan?.workers || executionWorkersFrom(projectedWorkers)
  const latestReview = projection.reviews.at(-1)
  const status = projection.status === 'pending' ? run.status : projection.status
  const selectedStrategy = String(projection.strategy?.selected || run.execution_strategy || 'auto')
  const isReact = selectedStrategy === 'react'
  const isMultiAgent = selectedStrategy === 'multi-agent-plan-execute-review'
  const reasonCodes = stringArray(projection.strategy?.reason_codes)
  const verdict = stringValue(latestReview?.verdict)

  return <section className={`runtime-trace runtime-${status}`} aria-live="polite" aria-label="Atlas 智能体运行指挥台">
    <div className="runtime-trace-head">
      <div className="runtime-trace-title">
        <span className="runtime-live-mark"><i /> ATLAS RUNTIME</span>
        <strong>{isMultiAgent ? '多智能体协作中' : isReact ? '单智能体执行中' : '受审查任务执行中'}</strong>
        <small>{strategyLabel[selectedStrategy] || selectedStrategy}</small>
      </div>
      <div className={`runtime-trace-state runtime-state-${status}`}>
        <i />{statusLabel[status] || status}
      </div>
    </div>

    <div className="runtime-trace-meta">
      <span>{run.execution_mode === 'langgraph' ? 'LANGGRAPH' : 'COMPAT'}</span>
      <span>RUN {shortId(run.run_id)}</span>
      <span>EVENT {projection.lastSequence}</span>
      <span>{connectionLabel[projection.connection.status] || projection.connection.status}</span>
    </div>

    {reasonCodes.length > 0 ? <div className="runtime-route-reasons" aria-label="策略选择原因">
      <b>为什么选择这个框架</b>
      {reasonCodes.slice(0, 4).map((reason) => <span key={reason}>{reasonLabel[reason] || reason}</span>)}
    </div> : null}

    {projection.fallback ? <p className="runtime-callout runtime-callout-danger" role="status">
      新运行时已安全降级：{String(projection.fallback.reason || '启动失败')}
    </p> : null}

    <div className={`runtime-orchestration ${isReact ? 'runtime-orchestration-react' : ''}`}>
      <AgentCard
        kind="orchestrator"
        eyebrow={isReact ? 'PRIMARY AGENT' : 'ORCHESTRATOR'}
        title={isReact ? 'ReAct Agent' : '任务编排者'}
        description={plan?.goal || (isReact ? '在有界循环中推理、选择工具并观察结果。' : '分析全局目标，拆解任务并生成执行 DAG。')}
        status={nodeStatus(projection, isReact ? ['react', 'direct_response'] : ['plan', 'execute'])}
      />

      {isReact ? <div className="runtime-react-loop" aria-label="ReAct 执行循环">
        <span>THINK</span><i>→</i><span>ACT</span><i>→</i><span>OBSERVE</span><i>↺</i>
      </div> : <>
        <div className="runtime-flow-connector"><span>{isMultiAgent ? '并行派发' : '顺序派发'}</span></div>
        <div className="runtime-worker-zone" aria-label="Subagents">
          <div className="runtime-zone-head">
            <div><span>EXECUTION TEAM</span><b>Subagents</b></div>
            <small>{planWorkers.length} 个 Worker · {plan?.tasks.length || 0} 个任务</small>
          </div>
          <div className="runtime-worker-grid">
            {planWorkers.length > 0 ? planWorkers.map((worker, index) => <WorkerCard
              key={worker.workerId}
              worker={worker}
              state={projectedWorkers[worker.workerId]}
              tasks={plan?.tasks.filter((task) => task.workerId === worker.workerId) || []}
              index={index}
            />) : <div className="runtime-worker-placeholder"><i /><span>Orchestrator 正在创建 Subagents…</span></div>}
          </div>
        </div>

        <div className="runtime-flow-connector runtime-flow-review"><span>结果与证据</span></div>
        <AgentCard
          kind="reviewer"
          eyebrow="INDEPENDENT REVIEWER"
          title={reviewer?.workerId || '等待创建 Reviewer'}
          description={reviewDescription(latestReview, plan)}
          status={reviewer?.status || nodeStatus(projection, ['create_reviewer', 'review'])}
          verdict={verdict}
        />
      </>}
    </div>

    {!isReact && projection.reviews.length > 0 ? <div className="runtime-decision-strip" aria-label="Reviewer 决策记录">
      <b>Review 决策</b>
      {projection.reviews.map((review, index) => {
        const eventType = stringValue(review.event_type) || 'review'
        const decision = stringValue(review.verdict) || reviewEventLabel(eventType)
        return <span key={`${eventType}-${index}`} className={`runtime-verdict runtime-verdict-${decision.toLowerCase()}`}>
          {decision}
        </span>
      })}
    </div> : null}

    {projection.escalation ? <p className="runtime-callout runtime-callout-warn" role="status">
      <b>需要你介入</b>{String(projection.escalation.reason || projection.escalation.required_actions || 'Reviewer 请求人工决策')}
    </p> : null}
    {projection.pendingInterrupt ? <p className="runtime-callout runtime-callout-warn" role="status">
      <b>写操作等待授权</b>{String(projection.pendingInterrupt.reason || projection.pendingInterrupt.tool_name || '请确认精确工具参数')}
    </p> : null}
    {projection.sideEffectBoundary ? <p className="runtime-safety-note" role="status">
      <span>◆</span> 已建立副作用安全边界，后续失败不会切换执行器重放
    </p> : null}

    {nodes.length > 0 ? <details className="runtime-trace-detail">
      <summary><span>运行步骤</span><small>{nodes.filter(([, state]) => state.status === 'succeeded').length}/{nodes.length} 完成</small></summary>
      <ol className="runtime-trace-nodes">
        {nodes.map(([name, state]) => <li key={name} className={`runtime-node runtime-node-${state.status}`}>
          <i /><span>{nodeLabel[name] || name}</span><small>{state.status === 'running' ? '进行中' : statusLabel[state.status] || state.status}</small>
        </li>)}
      </ol>
    </details> : null}

    {(retries.length > 0 || errors.length > 0 || projection.connection.error) ? <details className="runtime-trace-detail runtime-error-detail">
      <summary><span>{errors.length > 0 || projection.connection.error ? '异常与恢复' : '重试记录'}</span><small>{errors.length + retries.length}</small></summary>
      {retries.map(([target, retry]) => <p key={target}>{target} 第 {retry.attempt} 次重试{retry.delaySeconds !== undefined ? `，等待 ${retry.delaySeconds}s` : ''}</p>)}
      {errors.map((error, index) => <p key={`${error.message || 'error'}-${index}`}>{String(error.message || error.category || '运行错误')}</p>)}
      {projection.connection.error ? <p>事件连接：{projection.connection.error}</p> : null}
    </details> : null}
  </section>
}

function AgentCard({ kind, eyebrow, title, description, status, verdict }: {
  kind: 'orchestrator' | 'reviewer'
  eyebrow: string
  title: string
  description: string
  status: string
  verdict?: string
}) {
  return <article className={`runtime-agent-card runtime-agent-${kind}`}>
    <div className="runtime-agent-avatar" aria-hidden="true">{kind === 'reviewer' ? 'R' : 'O'}</div>
    <div className="runtime-agent-copy">
      <span>{eyebrow}</span>
      <strong>{title}</strong>
      <p>{description}</p>
    </div>
    <div className={`runtime-agent-status runtime-agent-status-${normalizedStatus(verdict || status)}`}>
      <i />{verdict || statusLabel[status] || status || '等待中'}
    </div>
  </article>
}

function WorkerCard({ worker, state, tasks, index }: {
  worker: PlanWorker
  state?: RuntimeWorkerState
  tasks: readonly PlanTask[]
  index: number
}) {
  const status = state?.status || 'created'
  const dependencyCount = tasks.reduce((total, task) => total + task.dependsOn.length, 0)
  return <article className={`runtime-worker-card runtime-worker-${normalizedStatus(status)}`}>
    <header>
      <div className="runtime-worker-avatar">{String.fromCharCode(65 + (index % 26))}</div>
      <div><span>WORKER {String(index + 1).padStart(2, '0')}</span><strong>{worker.role}</strong></div>
      <i className="runtime-worker-pulse" />
    </header>
    <p>{worker.objective}</p>
    {tasks.length > 0 ? <div className="runtime-worker-tasks">
      {tasks.slice(0, 2).map((task) => <span key={task.taskId} title={task.objective}>{task.objective}</span>)}
    </div> : null}
    <footer>
      <span>{statusLabel[status] || status}</span>
      <small>{dependencyCount > 0 ? `${dependencyCount} 个依赖` : '可独立执行'}</small>
      {worker.allowedTools.length > 0 ? <small>{worker.allowedTools.length} 个工具</small> : null}
    </footer>
  </article>
}

function runtimePlanFrom(value: JsonValue | undefined): RuntimePlan | null {
  const record = recordValue(value)
  if (!record) return null
  const workers = arrayValue(record.workers).flatMap((item) => {
    const worker = recordValue(item)
    const workerId = stringValue(worker?.worker_id)
    if (!worker || !workerId) return []
    return [{
      workerId,
      role: stringValue(worker.role) || workerId,
      objective: stringValue(worker.objective) || '执行已分配任务',
      allowedTools: stringArray(worker.allowed_tools),
    }]
  })
  const tasks = arrayValue(record.tasks).flatMap((item) => {
    const task = recordValue(item)
    const taskId = stringValue(task?.task_id)
    const workerId = stringValue(task?.worker_id)
    if (!task || !taskId || !workerId) return []
    return [{
      taskId,
      workerId,
      objective: stringValue(task.objective) || taskId,
      dependsOn: stringArray(task.depends_on),
    }]
  })
  return {
    goal: stringValue(record.goal) || 'Orchestrator 正在分析任务目标',
    version: numberValue(record.version),
    workers,
    tasks,
    acceptanceCriteria: stringArray(record.acceptance_criteria),
  }
}

function executionWorkersFrom(workers: Readonly<Record<string, RuntimeWorkerState>>): readonly PlanWorker[] {
  return Object.values(workers).filter((worker) => worker.role !== 'reviewer').map((worker) => ({
    workerId: worker.workerId,
    role: worker.role || worker.workerId,
    objective: '执行 Orchestrator 分配的子任务',
    allowedTools: [],
  }))
}

function reviewDescription(review: Record<string, JsonValue> | undefined, plan: RuntimePlan | null): string {
  if (review?.verdict) return `已审查 ${plan?.acceptanceCriteria.length || 0} 条验收标准，并给出 ${String(review.verdict)} 裁决。`
  if (review?.event_type === 'review.requested') return `正在独立核验目标、结果、证据与 ${plan?.acceptanceCriteria.length || 0} 条验收标准。`
  return '执行 Worker 不参与自审；Reviewer 使用独立上下文和只读权限。'
}

function nodeStatus(projection: RuntimeRunProjection, names: readonly string[]): string {
  const states = names.map((name) => projection.nodes[name]?.status).filter(Boolean)
  if (states.includes('running')) return 'running'
  if (states.includes('failed')) return 'failed'
  if (states.includes('succeeded')) return 'succeeded'
  return projection.status === 'pending' ? 'pending' : projection.status
}

function reviewEventLabel(eventType: string): string {
  if (eventType === 'review.requested') return 'REVIEWING'
  if (eventType === 'review.revision_requested') return 'REVISE'
  if (eventType === 'review.replan_requested') return 'REPLAN'
  return eventType.replace('review.', '').toUpperCase()
}

function normalizedStatus(status: string): string {
  if (['succeeded', 'complete', 'completed', 'PASS'].includes(status)) return 'succeeded'
  if (['running', 'REVIEWING'].includes(status)) return 'running'
  if (['failed', 'REJECT', 'timed_out'].includes(status)) return 'failed'
  if (['paused', 'ESCALATE'].includes(status)) return 'paused'
  return 'pending'
}

function recordValue(value: JsonValue | undefined): Record<string, JsonValue> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : null
}

function arrayValue(value: JsonValue | undefined): readonly JsonValue[] {
  return Array.isArray(value) ? value : []
}

function stringValue(value: JsonValue | undefined): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function stringArray(value: JsonValue | undefined): readonly string[] {
  return arrayValue(value).filter((item): item is string => typeof item === 'string')
}

function numberValue(value: JsonValue | undefined): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function shortId(value: string): string {
  return value.length > 10 ? `${value.slice(0, 6)}…${value.slice(-4)}` : value
}
