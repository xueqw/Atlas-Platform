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
  '做一个企业知识库问答助手，能回答制度和流程问题',
  '做一个客服助手，能根据知识库回答售后问题并生成工单摘要',
  '做一个销售线索助手，能总结客户信息并生成跟进计划',
  '做一个流程办理助手，能引导员工完成报销、入职和审批'
]

const buildSteps: BuildStep[] = [
  { title: '理解场景', desc: '识别业务目标、用户角色和投放边界' },
  { title: '设计 Agent', desc: '生成名称、提示词、能力范围和连接器' },
  { title: '编写文件', desc: '自动创建 manifest、主程序、Skill 和测试用例' },
  { title: '可运行草稿', desc: '进入 Web IDE 后可以直接预览和评测' }
]

const sleep = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))

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
  const [activeStep, setActiveStep] = useState(0)
  const [generated, setGenerated] = useState<GeneratedAgentApp | null>(null)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: crypto.randomUUID(),
      role: 'assistant',
      kind: 'build',
      content:
        '把你想做的 Agent 用一句话告诉我就行。我会自动拆需求、生成应用草稿、创建技能文件和测试样例，不需要你手动配表单。'
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
      appendAssistant('收到，我先把这句话转成可投放的 Agent 需求：明确服务对象、触发场景、输出结果和安全边界。')
      await sleep(420)
      setActiveStep(1)
      appendAssistant('正在设计 Agent 结构：名称、系统提示词、核心 Skill、连接器和需要生成的文件会一起准备好。')
      await sleep(520)
      setActiveStep(2)
      appendAssistant('开始自动创建项目文件：manifest.json、main.py、SKILL.md、tests.json。你不用手写初始代码。')

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
      notice('Agent 草稿已自动生成')
    } catch (error) {
      setMessages(old => [
        ...old,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          content: error instanceof Error ? `生成失败：${error.message}` : '生成失败，请检查后端服务。',
          status: 'done'
        }
      ])
      notice('生成失败，请检查后端服务')
    } finally {
      setBusy(false)
    }
  }

  function renderMessage(message: ChatMessage) {
    if (message.kind === 'result' && message.result) {
      return (
        <div className="agent-result-card">
          <div className="result-headline">
            <span>已生成可运行 Agent</span>
            <strong>{message.result.draft.name}</strong>
          </div>
          <p>{message.content}</p>
          <div className="result-grid">
            <div>
              <small>应用场景</small>
              <b>{message.result.blueprint.domain}</b>
            </div>
            <div>
              <small>生成文件</small>
              <b>{message.result.files.length} 个</b>
            </div>
            <div>
              <small>能力数量</small>
              <b>{message.result.blueprint.skills.length} 项</b>
            </div>
          </div>
          <div className="file-chips generated-files">
            {message.result.files.map(file => <b key={file}>{file}</b>)}
          </div>
          <button type="button" onClick={() => onOpenDraft(message.result!.draft)}>
            打开 Web IDE 查看自动生成内容
          </button>
        </div>
      )
    }

    if (message.kind === 'build') {
      return (
        <div className="agent-build-bubble">
          <span className="build-badge">自动构建</span>
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
          <strong>自动 Agent</strong>
          <button type="button" onClick={() => setInput('做一个新的企业智能体应用')}>+</button>
        </div>
        <div className="project-user-card auto-card">
          <span>Atlas</span>
          <b>{projectSummary.name}</b>
          <small>{generated ? '已生成可运行草稿' : '一句话生成 Agent 应用'}</small>
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
          <span>{generated ? '已创建文件，可进入 Web IDE 调整...' : '描述业务场景即可生成 Agent'}</span>
        </div>
      </aside>

      <main className="project-chat-main">
        <header className="project-chat-header auto-header">
          <div className="project-avatar">A</div>
          <div>
            <span className="auto-eyebrow">Conversational Agent Builder</span>
            <h1>{projectSummary.name}</h1>
            <p>像聊天一样自动生成 Agent：需求理解、能力设计、文件创建和 Web IDE 调整一步完成。</p>
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
                Atlas 正在自动搭建 Agent...
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
            placeholder="告诉 Atlas 你想自动创建什么 Agent，例如：做一个客服助手，能根据知识库回答售后问题并生成工单摘要..."
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                event.currentTarget.form?.requestSubmit()
              }
            }}
          />
          <div className="project-composer-actions">
            <span className="composer-hint">Enter 发送，Shift + Enter 换行</span>
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
          <span>自动生成项目</span>
          <h2>{projectSummary.name}</h2>
          <button type="button" disabled={!generated} onClick={() => generated && onOpenDraft(generated.draft)}>
            {generated ? '进入 Web IDE' : '等待生成'}
          </button>
        </div>

        <section className="settings-section">
          <div className="settings-section-title">
            <span>Agent 管理 · {projectSummary.agentCount}</span>
            <button type="button">自动添加</button>
          </div>
          <div className="agent-setting-item">
            <div className="bot-avatar">A</div>
            <div>
              <b>{generated?.draft.name || '等待生成 Agent'}</b>
              <small>{generated?.blueprint.domain || '对话后自动确定应用能力'}</small>
            </div>
            <span>{generated ? '已就绪 ›' : '待生成'}</span>
          </div>
        </section>

        <section className="settings-section auto-output-section">
          <span className="settings-label">自动产物</span>
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
          <span className="settings-label">生成文件</span>
          <div className="file-chips">
            {projectSummary.files.length ? projectSummary.files.map(file => <b key={file}>{file}</b>) : <b>对话后自动生成</b>}
          </div>
        </section>

        <section className="settings-section">
          <span className="settings-label">能力与连接器</span>
          <div className="skill-tags">
            {projectSummary.skills.map(item => <span key={item}>{item}</span>)}
            {projectSummary.connectors.map(item => <span key={item}>{item}</span>)}
            {!projectSummary.skills.length && <span>等待 Atlas 自动编排</span>}
          </div>
        </section>
      </aside>
    </section>
  )
}
