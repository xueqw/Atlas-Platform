import { useState } from 'react'
import AgentStudio from './AgentStudio'
import WebIDE from './WebIDE'
import type { Agent, KnowledgeBase } from './types'
import './webide.css'

export default function AppBuilder({
  agents,
  knowledge,
  refresh,
  notice
}: {
  agents: Agent[]
  knowledge: KnowledgeBase[]
  refresh: () => Promise<void> | void
  notice: (text: string) => void
}) {
  const [tab, setTab] = useState<'agent' | 'ide'>('agent')

  return (
    <section className="builder-page">
      <div className="builder-tabs">
        <button
          type="button"
          className={tab === 'agent' ? 'active' : ''}
          onClick={() => setTab('agent')}
        >
          智能体配置
        </button>

        <button
          type="button"
          className={tab === 'ide' ? 'active' : ''}
          onClick={() => setTab('ide')}
        >
          Web IDE
        </button>
      </div>

      <div className="builder-content">
        {tab === 'agent' ? (
          <AgentStudio
            agents={agents}
            knowledge={knowledge}
            refresh={refresh}
            notice={notice}
          />
        ) : (
          <WebIDE notice={notice} />
        )}
      </div>
    </section>
  )
}
