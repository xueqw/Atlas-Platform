import { FormEvent, useMemo, useState } from 'react'
import { generateAgentApp, type AppDraft, type GeneratedAgentApp } from './api'

type ChatMessage = {
  id: string
  role: 'assistant' | 'user'
  content: string
  status?: 'thinking' | 'done'
}

const starters = [
  '做一个企业知识库问答助手，能回答制度和流程问题',
  '做一个客服助手，能根据知识库回答售后问题并生成工单摘要',
  '做一个销售线索助手，能总结客户信息并生成跟进计划'
]

export default function AgentProjectChat({
  onOpenDraft,
  notice
}: {
  onOpenDraft: (draft: AppDraft) => void
  notice: (text: string) => void
}) {
  const [projectName, setProjectName] = useState('Atlas 企业助手项目')
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [generated, setGenerated] = useState<GeneratedAgentApp | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: crypto.randomUUID(),
      role: 'assistant',
      content:
        '你好，我是 Atlas 项目生成助手。告诉我你想投放什么 Agent，我会先确定应用名称、能力范围、Skill、连接器和初始代码。'
    }
  ])

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

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    const content = input.trim()
    if (!content || busy) return

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content
    }
    const thinkingMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'assistant',
      content: '正在理解需求、生成 Agent 草稿和初始文件...',
      status: 'thinking'
    }

    setMessages(old => [...old, userMessage, thinkingMessage])
    setInput('')
    setBusy(true)

    try {
      const result = await generateAgentApp(content, projectName)
      setGenerated(result)
      setProjectName(result.draft.name)
      setMessages(old =>
        old.map(item =>
          item.id === thinkingMessage.id
            ? { ...item, content: result.reply, status: 'done' }
            : item
        )
      )
      notice('Agent 草稿已生成')
    } catch (error) {
      setMessages(old =>
        old.map(item =>
          item.id === thinkingMessage.id
            ? {
                ...item,
                content: error instanceof Error ? `生成失败：${error.message}` : '生成失败',
                status: 'done'
              }
            : item
        )
      )
      notice('生成失败，请检查后端服务')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="project-chat-page">
      <aside className="project-list-pane">
        <div className="project-list-title">
          <strong>项目对话</strong>
          <button type="button" onClick={() => setInput('做一个新的企业智能体应用')}>+</button>
        </div>
        <div className="project-user-card">
          <span>Atlas</span>
          <b>{projectSummary.name}</b>
          <small>{generated ? '刚刚生成可运行草稿' : '等待描述投放场景'}</small>
        </div>
        <div className="project-history-item active">
          <b>{projectSummary.name}</b>
          <span>{generated ? '已创建文件，可进入 Web IDE 调整...' : '描述业务场景即可生成 Agent'}</span>
        </div>
      </aside>

      <main className="project-chat-main">
        <header className="project-chat-header">
          <div className="project-avatar">A</div>
          <div>
            <h1>{projectSummary.name}</h1>
            <p>通过对话生成可运行、可评测、可继续投放的 Atlas Agent 应用草稿。</p>
          </div>
        </header>

        <div className="project-messages">
          {messages.map(message => (
            <div className={`project-message ${message.role}`} key={message.id}>
              {message.role === 'assistant' && <span className="bot-avatar">A</span>}
              <div>
                {message.status === 'thinking' && <i className="typing-dot" />}
                <p>{message.content}</p>
              </div>
            </div>
          ))}
        </div>

        <form className="project-composer" onSubmit={submit}>
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
            placeholder="输入你想创建的 Agent，例如：做一个企业知识库问答助手，能回答制度和流程问题..."
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                event.currentTarget.form?.requestSubmit()
              }
            }}
          />
          <div className="project-composer-actions">
            <button type="button">+</button>
            <button type="button">@</button>
            <button className="send-project" disabled={busy || !input.trim()}>
              ↑
            </button>
          </div>
        </form>
      </main>

      <aside className="project-settings-pane">
        <button className="close-settings" type="button">×</button>
        <div className="project-settings-card center-card">
          <div className="project-large-avatar">A</div>
          <h2>{projectSummary.name}</h2>
          <button type="button">修改项目信息</button>
        </div>

        <section className="settings-section">
          <div className="settings-section-title">
            <span>Agent 管理 · {projectSummary.agentCount}</span>
            <button type="button">+ 添加</button>
          </div>
          <div className="agent-setting-item">
            <div className="bot-avatar">A</div>
            <div>
              <b>{generated?.draft.name || '等待生成 Agent'}</b>
              <small>{generated?.blueprint.domain || '通过对话生成应用能力'}</small>
            </div>
            <span>共{projectSummary.agentCount}个 ›</span>
          </div>
        </section>

        <section className="settings-section">
          <span className="settings-label">生成文件</span>
          <div className="file-chips">
            {projectSummary.files.length ? projectSummary.files.map(file => <b key={file}>{file}</b>) : <b>暂无文件</b>}
          </div>
        </section>

        <section className="settings-section">
          <span className="settings-label">能力与连接器</span>
          <div className="skill-tags">
            {projectSummary.skills.map(item => <span key={item}>{item}</span>)}
            {projectSummary.connectors.map(item => <span key={item}>{item}</span>)}
            {!projectSummary.skills.length && <span>等待生成</span>}
          </div>
        </section>

        <section className="settings-section">
          <button
            type="button"
            className="open-ide-button"
            disabled={!generated}
            onClick={() => generated && onOpenDraft(generated.draft)}
          >
            进入 Web IDE 调整代码
          </button>
        </section>
      </aside>
    </section>
  )
}
