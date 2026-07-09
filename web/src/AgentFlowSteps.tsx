import type { Agent } from './types'

type StepStatus = 'done' | 'todo' | 'warn' | 'archived'

type FlowStep = {
  key: string
  label: string
  status: StepStatus
  onClick: () => void
}

const statusText: Record<StepStatus, string> = {
  done: '已完成',
  todo: '未开始',
  warn: '有问题',
  archived: '已下架',
}

function evaluateStatus(agent: Agent): StepStatus {
  if (agent.kind === 'prompt') return 'done'
  if (agent.last_eval_ok === null) return 'todo'
  return agent.last_eval_ok ? 'done' : 'warn'
}

export default function AgentFlowSteps({
  agent,
  onOpenCode,
  onOpenPrompt,
  onDeployConfig,
  onPublishChecklist,
}: {
  agent: Agent
  onOpenCode: (agent: Agent) => void
  onOpenPrompt: (agent: Agent) => void
  onDeployConfig: (agent: Agent) => void
  onPublishChecklist: (agent: Agent) => void
}) {
  const openBuild = () => (agent.kind === 'code' ? onOpenCode(agent) : onOpenPrompt(agent))

  const steps: FlowStep[] = [
    {
      key: 'develop',
      label: '开发',
      status: agent.version_no >= 1 ? 'done' : 'todo',
      onClick: openBuild,
    },
    {
      key: 'test',
      label: '测试',
      status: agent.has_passed_test ? 'done' : 'todo',
      onClick: openBuild,
    },
    {
      key: 'evaluate',
      label: '评测',
      status: evaluateStatus(agent),
      onClick: openBuild,
    },
    {
      key: 'deploy-config',
      label: '落地配置',
      status: agent.deploy_config_configured ? 'done' : 'todo',
      onClick: () => onDeployConfig(agent),
    },
    {
      key: 'publish',
      label: '发布',
      status: agent.status === 'published' ? 'done' : agent.status === 'archived' ? 'archived' : 'todo',
      onClick: () => onPublishChecklist(agent),
    },
  ]

  return (
    <div className="agent-flow-steps">
      {steps.map((step, index) => (
        <button
          type="button"
          key={step.key}
          className={`agent-flow-step ${step.status}`}
          onClick={step.onClick}
          title={`${step.label} · ${statusText[step.status]}`}
        >
          <i>{index + 1}</i>
          <span>{step.label}</span>
        </button>
      ))}
    </div>
  )
}
