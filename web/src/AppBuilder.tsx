import { useState } from 'react'
import AgentProjectChat from './AgentProjectChat'
import AgentsList from './AgentsList'
import AgentStudio from './AgentStudio'
import type { Agent, KnowledgeBase } from './types'
import type { AppDraft } from './api'
import './webide.css'

export default function AppBuilder({
  agents,
  knowledge,
  refresh,
  notice,
  onInvokeAgent,
}: {
  agents: Agent[]
  knowledge: KnowledgeBase[]
  refresh: () => Promise<void> | void
  notice: (text: string) => void
  onInvokeAgent: (agent: Agent) => void
}) {
  const [tab, setTab] = useState<'agents' | 'project' | 'agent'>('agents')
  const [openedDraft, setOpenedDraft] = useState<AppDraft | null>(null)
  const [openedAgentId, setOpenedAgentId] = useState('')

  function openDraft(draft: AppDraft) {
    setOpenedDraft(draft)
    setTab('project')
  }

  function openCodeAgent(agent: Agent) {
    openDraft({ id: agent.id, name: agent.name, status: agent.status })
  }

  function openPromptAgent(agent: Agent) {
    setOpenedAgentId(agent.id)
    setTab('agent')
  }

  return (
    <section className="builder-page">
      <div className="builder-tabs">
        <button
          type="button"
          className={tab === 'agents' ? 'active' : ''}
          onClick={() => setTab('agents')}
        >
          我的 Agents
        </button>

        <button
          type="button"
          className={tab === 'project' ? 'active' : ''}
          onClick={() => setTab('project')}
        >
          项目对话生成
        </button>
      </div>

      <div className="builder-content">
        {tab === 'agents' ? (
          <AgentsList
            onOpenCode={openCodeAgent}
            onOpenPrompt={openPromptAgent}
            onInvoke={onInvokeAgent}
            onNewProject={() => {
              setOpenedDraft(null)
              setTab('project')
            }}
            notice={notice}
          />
        ) : tab === 'project' ? (
          <AgentProjectChat
            onGoToAgentsList={() => setTab('agents')}
            knowledgeBases={knowledge}
            notice={notice}
            initialDraft={openedDraft}
          />
        ) : tab === 'agent' ? (
          <AgentStudio
            agents={agents}
            knowledge={knowledge}
            refresh={refresh}
            notice={notice}
            initialAgentId={openedAgentId}
          />
        ) : null}
      </div>
    </section>
  )
}
