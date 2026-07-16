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
  return (
    <section className="gateway-builder">
      <header className="gateway-builder-header">
        <div>
          <span>APPLICATION DEVELOPMENT</span>
          <h1>智能体开发工作台</h1>
          <p>规划、DAG 编排、测试、评测、监控和发布统一在一个开发模块中完成。</p>
        </div>
        <a href={`${gatewayOrigin}/workbench`} target="_blank" rel="noreferrer">在新窗口打开</a>
      </header>
      <iframe
        className="gateway-builder-frame"
        src={`${gatewayOrigin}/workbench`}
        title="AgentGateway 智能体开发工作台"
        allow="clipboard-read; clipboard-write"
      />
    </section>
  )
}
