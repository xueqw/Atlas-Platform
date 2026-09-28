import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { generateAgentApp, type AppDraft, type GeneratedAgentApp } from './api'

type ChatMessage = {
  id: string
  role: 'assistant' | 'user'
  content: string
  status?: 'thinking' | 'done'
  kind?: 'text' | 'build' | 'result'
  result?: GeneratedAgentApp
}

type BuildStep = {
  title: string
  desc: string
}

const starters = [
  'Build a knowledge base assistant for company policies and procedures',
  'Build a customer support agent that answers questions and summarizes tickets',
  'Build a sales agent that qualifies leads and creates follow-up plans',
  'Build an engineering agent that reviews GitHub issues and pull requests'
]

const buildSteps: BuildStep[] = [
  { title: 'Understand', desc: 'Identify the business goal, users, and operating boundaries' },
  { title: 'Design', desc: 'Generate the name, prompt, capabilities, and connectors' },
  { title: 'Build', desc: 'Create the manifest, app code, skill spec, and tests' },
  { title: 'Run', desc: 'Preview and evaluate the draft in the Web IDE' }
]

const sleep = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))

export default function AgentProjectChat({
  onOpenDraft,
  notice
}: {
  onOpenDraft: (draft: AppDraft) => void
  notice: (text: string) => void
}) {
  const [projectName, setProjectName] = useState('Atlas Business Agent Project')
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [activeStep, setActiveStep] = useState(0)
  const [generated, setGenerated] = useState<GeneratedAgentApp | null>(null)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: crypto.randomUUID(),
      role: 'assistant',
      kind: 'build',
      content:
        'Describe the agent you want in one sentence. Atlas will turn it into a runnable draft with a skill spec and evaluation cases.'
    }
  ])

  useEffect(() => {
    messagesRef.current?.scrollTo({ top: messagesRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, activeStep])

  const projectSummary = useMemo(() => {
    if (!generated) {
      return {
        name: projectName,
        agentCount: 0,
        files: [] as string[],
        skills: [] as string[],
        connectors: [] as string[]
      }
    }

    return {
      name: generated.draft.name,
      agentCount: 1,
      files: generated.files,
      skills: generated.blueprint.skills,
      connectors: generated.blueprint.connectors
    }
  }, [generated, projectName])

  function appendAssistant(content: string, kind: ChatMessage['kind'] = 'build') {
    setMessages(old => [
      ...old,
      {
        id: crypto.randomUUID(),
        role: 'assistant',
        content,
        kind
      }
    ])
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    const content = input.trim()
    if (!content || busy) return

    setMessages(old => [
      ...old,
      {
        id: crypto.randomUUID(),
        role: 'user',
        content
      }
    ])
    setInput('')
    setBusy(true)
    setGenerated(null)
    setActiveStep(0)

    try {
      appendAssistant('Got it. I am turning that request into an agent specification with users, triggers, outputs, and safety boundaries.')
      await sleep(420)
      setActiveStep(1)
      appendAssistant('Designing the agent structure: name, system prompt, core skills, connectors, and project files.')
      await sleep(520)
      setActiveStep(2)
      appendAssistant('Creating manifest.json, main.py, SKILL.md, and tests.json. The first runnable draft is generated for you.')

      const result = await generateAgentApp(content, '')
      setGenerated(result)
      setProjectName(result.draft.name)
      setActiveStep(3)
      setMessages(old => [
        ...old,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          kind: 'result',
          content: result.reply,
          result
        }
      ])
      notice('Agent draft created')
    } catch (error) {
      setMessages(old => [
        ...old,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          content: error instanceof Error ? `Generation failed: ${error.message}` : 'Generation failed. Check the API service.',
          status: 'done'
        }
      ])
      notice('Generation failed. Check the API service.')
    } finally {
      setBusy(false)
    }
  }

  function renderMessage(message: ChatMessage) {
    if (message.kind === 'result' && message.result) {
      return (
        <div className="agent-result-card">
          <div className="result-headline">
            <span>Runnable agent created</span>
            <strong>{message.result.draft.name}</strong>
          </div>
          <p>{message.content}</p>
          <div className="result-grid">
            <div>
              <small>Use case</small>
              <b>{message.result.blueprint.domain}</b>
            </div>
            <div>
              <small>Files</small>
              <b>{message.result.files.length}</b>
            </div>
            <div>
              <small>Capabilities</small>
              <b>{message.result.blueprint.skills.length}</b>
            </div>
          </div>
          <div className="file-chips generated-files">
            {message.result.files.map(file => <b key={file}>{file}</b>)}
          </div>
          <button type="button" onClick={() => onOpenDraft(message.result!.draft)}>
            Open in Web IDE
          </button>
        </div>
      )
    }

    if (message.kind === 'build') {
      return (
        <div className="agent-build-bubble">
          <span className="build-badge">Auto build</span>
          <p>{message.content}</p>
        </div>
      )
    }

    return <p>{message.content}</p>
  }

  return (
    <section className="project-chat-page auto-agent-page">
      <aside className="project-list-pane">
        <div className="project-list-title">
          <strong>Agent Builder</strong>
          <button type="button" onClick={() => setInput('Build a new business AI agent')}>+</button>
        </div>
        <div className="project-user-card auto-card">
          <span>Atlas</span>
          <b>{projectSummary.name}</b>
          <small>{generated ? 'Runnable draft ready' : 'Create an agent from one sentence'}</small>
        </div>
        <div className="auto-steps-mini">
          {buildSteps.map((step, index) => (
            <div className={index <= activeStep ? 'active' : ''} key={step.title}>
              <i>{index + 1}</i>
              <span>{step.title}</span>
            </div>
          ))}
        </div>
        <div className="project-history-item active">
          <b>{projectSummary.name}</b>
          <span>{generated ? 'Files created — open the Web IDE to refine them' : 'Describe a use case to generate an agent'}</span>
        </div>
      </aside>

      <main className="project-chat-main">
        <header className="project-chat-header auto-header">
          <div className="project-avatar">A</div>
          <div>
            <span className="auto-eyebrow">Conversational Agent Builder</span>
            <h1>{projectSummary.name}</h1>
            <p>Build an agent conversationally, from requirements and capabilities to runnable files and evaluation.</p>
          </div>
        </header>

        <div className="auto-progress-strip">
          {buildSteps.map((step, index) => (
            <div className={index <= activeStep ? 'active' : ''} key={step.title}>
              <b>{step.title}</b>
              <span>{step.desc}</span>
            </div>
          ))}
        </div>

        <div className="project-messages" ref={messagesRef}>
          {messages.map(message => (
            <div className={`project-message ${message.role}`} key={message.id}>
              {message.role === 'assistant' && <span className="bot-avatar">A</span>}
              <div>{renderMessage(message)}</div>
            </div>
          ))}
          {busy && (
            <div className="project-message assistant">
              <span className="bot-avatar">A</span>
              <div className="agent-thinking">
                <i className="typing-dot" />
                Atlas is building your agent...
              </div>
            </div>
          )}
        </div>

        <form className="project-composer auto-composer" onSubmit={submit}>
          <div className="starter-row">
            {starters.map(item => (
              <button type="button" key={item} onClick={() => setInput(item)}>
                {item}
              </button>
            ))}
          </div>
          <textarea
            value={input}
            onChange={event => setInput(event.target.value)}
            placeholder="Tell Atlas what to build, for example: Create a customer support agent that answers from our knowledge base and summarizes tickets."
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                event.currentTarget.form?.requestSubmit()
              }
            }}
          />
          <div className="project-composer-actions">
            <span className="composer-hint">Enter to send · Shift + Enter for a new line</span>
            <button className="send-project" disabled={busy || !input.trim()}>
              {busy ? '...' : '↑'}
            </button>
          </div>
        </form>
      </main>

      <aside className="project-settings-pane auto-settings-pane">
        <button className="close-settings" type="button">×</button>
        <div className="project-settings-card center-card auto-summary-card">
          <div className="project-large-avatar">A</div>
          <span>Generated project</span>
          <h2>{projectSummary.name}</h2>
          <button type="button" disabled={!generated} onClick={() => generated && onOpenDraft(generated.draft)}>
            {generated ? 'Open Web IDE' : 'Waiting for a prompt'}
          </button>
        </div>

        <section className="settings-section">
          <div className="settings-section-title">
            <span>Agents · {projectSummary.agentCount}</span>
            <button type="button">Auto add</button>
          </div>
          <div className="agent-setting-item">
            <div className="bot-avatar">A</div>
            <div>
              <b>{generated?.draft.name || 'Waiting for an agent'}</b>
              <small>{generated?.blueprint.domain || 'Capabilities are inferred from your prompt'}</small>
            </div>
            <span>{generated ? 'Ready ›' : 'Not generated'}</span>
          </div>
        </section>

        <section className="settings-section auto-output-section">
          <span className="settings-label">Build progress</span>
          <div className="output-list">
            {buildSteps.map((step, index) => (
              <div className={index <= activeStep ? 'active' : ''} key={step.title}>
                <i>{index < activeStep || generated ? '✓' : index + 1}</i>
                <span>{step.title}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="settings-section">
          <span className="settings-label">Generated files</span>
          <div className="file-chips">
            {projectSummary.files.length ? projectSummary.files.map(file => <b key={file}>{file}</b>) : <b>Generated after your prompt</b>}
          </div>
        </section>

        <section className="settings-section">
          <span className="settings-label">Capabilities and connectors</span>
          <div className="skill-tags">
            {projectSummary.skills.map(item => <span key={item}>{item}</span>)}
            {projectSummary.connectors.map(item => <span key={item}>{item}</span>)}
            {!projectSummary.skills.length && <span>Waiting for Atlas to configure the agent</span>}
          </div>
        </section>
      </aside>
    </section>
  )
}
