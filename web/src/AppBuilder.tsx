import { useState } from 'react'
import AgentProjectChat from './AgentProjectChat'
import AgentStudio from './AgentStudio'
import WebIDE from './WebIDE'
import type { Agent, KnowledgeBase } from './types'
import type { AppDraft } from './api'
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
  const [tab, setTab] = useState<'project' | 'agent' | 'ide'>('project')
  const [openedDraft, setOpenedDraft] = useState<AppDraft | null>(null)

  function openDraft(draft: AppDraft) {
    setOpenedDraft(draft)
    setTab('ide')
  }

  return (
    <section className="builder-page">
      <div className="builder-tabs">
        <button
          type="button"
          className={tab === 'project' ? 'active' : ''}
          onClick={() => setTab('project')}
        >
          Generate App
        </button>

        <button
          type="button"
          className={tab === 'agent' ? 'active' : ''}
          onClick={() => setTab('agent')}
        >
          Agent Settings
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
        {tab === 'project' ? (
          <AgentProjectChat onOpenDraft={openDraft} notice={notice} />
        ) : tab === 'agent' ? (
          <AgentStudio
            agents={agents}
            knowledge={knowledge}
            refresh={refresh}
            notice={notice}
          />
        ) : (
          <WebIDE notice={notice} initialDraft={openedDraft} />
        )}
      </div>
    </section>
  )
}
