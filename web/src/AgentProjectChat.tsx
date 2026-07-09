import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import {
  evaluateDraftApp,
  generateAgentApp,
  getDraftFile,
  listDraftFiles,
  listConnectors,
  listSkills,
  runDraftApp,
  saveDraftFile,
  validateDraftManifest,
  type AppDraft,
  type AppFile,
  type EvaluationCase,
  type EvaluationResult,
  type ManifestValidation,
  type GeneratedAgentApp
} from './api'
import type { Connector, KnowledgeBase, Skill } from './types'

type ChatMessage = {
  id: string
  role: 'assistant' | 'user'
  content: string
  status?: 'thinking' | 'done'
  kind?: 'text' | 'build' | 'result' | 'error'
  result?: GeneratedAgentApp
  retryInput?: string
}

type BuildStep = {
  title: string
  desc: string
}

type FlowPage = 'chat' | 'resources' | 'test' | 'review' | 'publish'

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
  { title: '可运行草稿', desc: '在右侧预览运行，在左侧高级编辑' }
]

const sleep = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))

export default function AgentProjectChat({
  onGoToAgentsList,
  knowledgeBases,
  notice,
  initialMessage,
  initialDraft
}: {
  onGoToAgentsList: () => void
  knowledgeBases: KnowledgeBase[]
  notice: (text: string) => void
  initialMessage?: { text: string; nonce: number } | null
  initialDraft?: AppDraft | null
}) {
  const [projectName, setProjectName] = useState('Atlas 企业助手项目')
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [activeStep, setActiveStep] = useState(0)
  const [generated, setGenerated] = useState<GeneratedAgentApp | null>(null)
  const [activeDraft, setActiveDraft] = useState<AppDraft | null>(initialDraft || null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [fileTree, setFileTree] = useState<AppFile[]>([])
  const [selectedFile, setSelectedFile] = useState('')
  const [fileContent, setFileContent] = useState('')
  const [savingFile, setSavingFile] = useState(false)
  const [runInput, setRunInput] = useState('Atlas')
  const [runLogs, setRunLogs] = useState('等待运行。右侧会显示 Agent 回复、步骤状态和沙箱日志。')
  const [runBusy, setRunBusy] = useState(false)
  const [flowPage, setFlowPage] = useState<FlowPage>('chat')
  const [skills, setSkills] = useState<Skill[]>([])
  const [connectors, setConnectors] = useState<Connector[]>([])
  const [selectedKnowledge, setSelectedKnowledge] = useState<string[]>([])
  const [selectedSkills, setSelectedSkills] = useState<string[]>([])
  const [selectedConnectors, setSelectedConnectors] = useState<string[]>([])
  const [savingResources, setSavingResources] = useState(false)
  const [validation, setValidation] = useState<ManifestValidation | null>(null)
  const [evaluating, setEvaluating] = useState(false)
  const [evaluation, setEvaluation] = useState<EvaluationResult | null>(null)
  const [published, setPublished] = useState(false)
  const [testCases, setTestCases] = useState<EvaluationCase[]>([
    { name: '基础任务', input: 'Atlas', expected: 'Atlas' }
  ])
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
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

  useEffect(() => {
    void loadResources()
  }, [])

  useEffect(() => {
    if (initialMessage) setInput(initialMessage.text)
  }, [initialMessage?.nonce])

  useEffect(() => {
    if (initialDraft?.id) {
      setActiveDraft(initialDraft)
      setGenerated(null)
      setProjectName(initialDraft.name || 'Agent 草稿')
      setEditorOpen(true)
      void loadDraftFiles(initialDraft)
    }
  }, [initialDraft?.id])

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

  const flowSteps: Array<{ key: FlowPage; title: string; desc: string }> = [
    { key: 'chat', title: '1 会话生成', desc: generated || activeDraft ? '草稿已建立' : '描述需求' },
    { key: 'resources', title: '2 资源配置', desc: `${selectedSkills.length} Skills / ${selectedKnowledge.length} 知识库` },
    { key: 'test', title: '3 测试运行', desc: evaluation ? `${evaluation.passed}/${evaluation.total} 通过` : '运行样例' },
    { key: 'review', title: '4 发布检查', desc: validation?.ok ? '校验通过' : '等待校验' },
    { key: 'publish', title: '5 发布', desc: published ? '已完成' : '确认上线' }
  ]

  async function loadResources() {
    try {
      const [skillItems, connectorItems] = await Promise.all([
        listSkills(),
        listConnectors()
      ])
      setSkills(skillItems)
      setConnectors(connectorItems)
    } catch (error) {
      notice('资源列表加载失败')
    }
  }

  function toggleSelected(value: string, setter: (updater: (old: string[]) => string[]) => void) {
    setter(old => old.includes(value) ? old.filter(item => item !== value) : [...old, value])
  }

  function parseManifest(text: string) {
    try {
      return JSON.parse(text || '{}') as Record<string, unknown>
    } catch {
      return {}
    }
  }

  function hydrateResourceConfig(manifest: Record<string, unknown>) {
    const knowledge = Array.isArray(manifest.knowledge_bases) ? manifest.knowledge_bases.filter((item): item is string => typeof item === 'string') : []
    const skillList = Array.isArray(manifest.skills) ? manifest.skills.filter((item): item is string => typeof item === 'string') : []
    const connectorList = Array.isArray(manifest.connectors) ? manifest.connectors.filter((item): item is string => typeof item === 'string') : []
    setSelectedKnowledge(knowledge)
    setSelectedSkills(skillList)
    setSelectedConnectors(connectorList)
  }

  async function readManifestText() {
    if (!activeDraft?.id) return '{}'
    if (selectedFile === 'manifest.json') return fileContent || '{}'
    try {
      const result = await getDraftFile(activeDraft.id, 'manifest.json')
      return result.content || '{}'
    } catch {
      return '{}'
    }
  }

  async function saveResourceConfig() {
    if (!activeDraft?.id) {
      notice('请先生成 Agent 草稿')
      return
    }
    setSavingResources(true)
    try {
      const manifest = parseManifest(await readManifestText())
      const next = {
        ...manifest,
        knowledge_bases: selectedKnowledge,
        skills: selectedSkills,
        connectors: selectedConnectors
      }
      const content = JSON.stringify(next, null, 2)
      await saveDraftFile(activeDraft.id, 'manifest.json', content)
      if (selectedFile === 'manifest.json') setFileContent(content)
      const result = await validateDraftManifest(activeDraft.id)
      setValidation(result)
      notice('资源配置已写入 manifest')
    } catch (error) {
      notice('资源配置保存失败')
    } finally {
      setSavingResources(false)
    }
  }

  async function runEvaluationFlow() {
    if (!activeDraft?.id) {
      notice('请先生成 Agent 草稿')
      return
    }
    setEvaluating(true)
    try {
      await saveResourceConfig()
      const result = await evaluateDraftApp(activeDraft.id, testCases)
      setEvaluation(result)
      notice(`测试完成：${result.passed}/${result.total}`)
      setFlowPage('review')
    } catch (error) {
      notice('测试失败，请查看运行日志')
    } finally {
      setEvaluating(false)
    }
  }

  async function runPublishCheck() {
    if (!activeDraft?.id) {
      notice('请先生成 Agent 草稿')
      return
    }
    try {
      await saveResourceConfig()
      const result = await validateDraftManifest(activeDraft.id)
      setValidation(result)
      notice(result.ok ? '发布检查通过' : '发布检查发现阻断项')
    } catch {
      notice('发布检查失败')
    }
  }

  function updateTestCase(index: number, patch: Partial<EvaluationCase>) {
    setTestCases(old => old.map((item, i) => i === index ? { ...item, ...patch } : item))
  }

  async function loadDraftFiles(draft: AppDraft) {
    try {
      const tree = await listDraftFiles(draft.id)
      setFileTree(tree)
      const firstPath = tree.find(item => item.path === 'manifest.json')?.path || tree[0]?.path || ''
      if (firstPath) await openDraftFile(draft.id, firstPath)
      try {
        const manifest = await getDraftFile(draft.id, 'manifest.json')
        hydrateResourceConfig(parseManifest(manifest.content))
      } catch {}
      notice(`已打开 ${draft.name} 的高级编辑`)
    } catch (error) {
      notice('读取草稿文件失败')
    }
  }

  async function openDraftFile(draftId: string, path: string) {
    setSelectedFile(path)
    try {
      const result = await getDraftFile(draftId, path)
      setFileContent(result.content)
      if (path === 'manifest.json') hydrateResourceConfig(parseManifest(result.content))
    } catch (error) {
      setFileContent('')
      notice('读取文件失败')
    }
  }

  async function saveCurrentFile() {
    if (!activeDraft?.id || !selectedFile) return
    setSavingFile(true)
    try {
      await saveDraftFile(activeDraft.id, selectedFile, fileContent)
      notice(`已保存 ${selectedFile}`)
    } catch (error) {
      notice('保存失败')
    } finally {
      setSavingFile(false)
    }
  }

  async function runActiveDraft() {
    const text = runInput.trim()
    if (!text || runBusy) return
    setRunBusy(true)
    setRunLogs('正在启动 Agent 运行：校验 manifest -> 组装上下文 -> 执行沙箱。')
    try {
      if (activeDraft?.id && selectedFile) await saveCurrentFile()
      if (activeDraft?.id) {
        const result = await runDraftApp(activeDraft.id, text)
        setRunLogs(result.logs || '运行结束，但没有返回日志。')
        notice(result.ok ? 'Agent 运行完成' : 'Agent 运行失败')
      } else {
        setRunLogs(`Mock run\n> input: ${text}\nAgent 已收到任务，生成草稿后即可接入真实运行。`)
      }
    } catch (error) {
      setRunLogs(error instanceof Error ? error.message : '运行失败')
      notice('Agent 运行失败')
    } finally {
      setRunBusy(false)
    }
  }

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
      setActiveDraft(result.draft)
      setRunInput(result.blueprint.sample_input || content)
      setTestCases([{ name: '生成样例', input: result.blueprint.sample_input || content, expected: result.blueprint.domain || result.draft.name }])
      setProjectName(result.draft.name)
      setActiveStep(3)
      setEditorOpen(true)
      setFlowPage('resources')
      void loadDraftFiles(result.draft)
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
          kind: 'error',
          content: error instanceof Error ? error.message : '生成失败，请检查后端服务。',
          retryInput: content
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
          <button type="button" onClick={() => {
            setActiveDraft(message.result!.draft)
            setEditorOpen(true)
            void loadDraftFiles(message.result!.draft)
          }}>
            打开左侧高级编辑
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

    if (message.kind === 'error') {
      return (
        <div className="agent-failure-card">
          <strong>生成失败</strong>
          <p>{message.content}</p>
          <div>
            <button
              type="button"
              onClick={() => {
                setInput(message.retryInput || '')
                inputRef.current?.focus()
              }}
            >
              重新生成
            </button>
            <button type="button" onClick={onGoToAgentsList}>前往我的 Agents 查看</button>
          </div>
        </div>
      )
    }

    return <p>{message.content}</p>
  }

  function renderResourcePickers(compact = false) {
    return (
      <div className={compact ? 'resource-picker compact' : 'resource-picker'}>
        <section>
          <div className="resource-picker-head">
            <b>Skills</b>
            <span>{selectedSkills.length}</span>
          </div>
          <div className="resource-options">
            {skills.map(skill => (
              <label key={skill.id} className={selectedSkills.includes(skill.name) ? 'checked' : ''}>
                <input
                  type="checkbox"
                  checked={selectedSkills.includes(skill.name)}
                  onChange={() => toggleSelected(skill.name, setSelectedSkills)}
                />
                <span>
                  <strong>{skill.name}</strong>
                  <small>{skill.description || skill.type}</small>
                </span>
              </label>
            ))}
            {!skills.length && <p>暂无 Skill，先到资源中心创建。</p>}
          </div>
        </section>

        <section>
          <div className="resource-picker-head">
            <b>知识库</b>
            <span>{selectedKnowledge.length}</span>
          </div>
          <div className="resource-options">
            {knowledgeBases.map(kb => (
              <label key={kb.id} className={selectedKnowledge.includes(kb.id) ? 'checked' : ''}>
                <input
                  type="checkbox"
                  checked={selectedKnowledge.includes(kb.id)}
                  onChange={() => toggleSelected(kb.id, setSelectedKnowledge)}
                />
                <span>
                  <strong>{kb.name}</strong>
                  <small>{kb.documents.length} 个文档</small>
                </span>
              </label>
            ))}
            {!knowledgeBases.length && <p>暂无知识库，先到资源中心创建。</p>}
          </div>
        </section>

        <section>
          <div className="resource-picker-head">
            <b>连接器</b>
            <span>{selectedConnectors.length}</span>
          </div>
          <div className="resource-options">
            {connectors.map(connector => (
              <label key={connector.provider} className={selectedConnectors.includes(connector.provider) ? 'checked' : ''}>
                <input
                  type="checkbox"
                  checked={selectedConnectors.includes(connector.provider)}
                  onChange={() => toggleSelected(connector.provider, setSelectedConnectors)}
                />
                <span>
                  <strong>{connector.name}</strong>
                  <small>{connector.connected ? '已连接' : connector.configured ? '待重连' : '未配置'}</small>
                </span>
              </label>
            ))}
            {!connectors.length && <p>暂无连接器。</p>}
          </div>
        </section>

        <button className="resource-save" type="button" onClick={saveResourceConfig} disabled={savingResources || !activeDraft}>
          {savingResources ? '保存中' : '保存资源配置'}
        </button>
      </div>
    )
  }

  function renderFlowMain() {
    if (flowPage === 'chat') {
      return (
        <>
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
              ref={inputRef}
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
        </>
      )
    }

    if (flowPage === 'resources') {
      return (
        <div className="flow-page resource-flow-page">
          <div className="flow-page-title">
            <span>CONFIGURE</span>
            <h2>绑定 Agent 运行资源</h2>
            <p>这里决定运行时可用的 Skill、知识库和外部工具。保存后会写入草稿的 manifest。</p>
          </div>
          {renderResourcePickers()}
          <div className="flow-page-actions">
            <button type="button" onClick={() => setFlowPage('chat')}>返回会话</button>
            <button type="button" className="primary" onClick={async () => { await saveResourceConfig(); setFlowPage('test') }}>进入测试</button>
          </div>
        </div>
      )
    }

    if (flowPage === 'test') {
      return (
        <div className="flow-page">
          <div className="flow-page-title row">
            <div>
              <span>TEST</span>
              <h2>样例测试</h2>
              <p>用固定样例测试 Agent 输出稳定性。测试结果会进入发布检查。</p>
            </div>
            <button type="button" className="primary" onClick={runEvaluationFlow} disabled={evaluating || !activeDraft}>
              {evaluating ? '测试中' : '运行全部测试'}
            </button>
          </div>
          <div className="test-case-table">
            {testCases.map((item, index) => (
              <div className="test-case-row" key={index}>
                <input value={item.name} onChange={event => updateTestCase(index, { name: event.target.value })} />
                <textarea value={item.input} onChange={event => updateTestCase(index, { input: event.target.value })} />
                <textarea value={item.expected} onChange={event => updateTestCase(index, { expected: event.target.value })} />
                <button type="button" onClick={() => setTestCases(old => old.filter((_, i) => i !== index))}>删除</button>
              </div>
            ))}
          </div>
          <button type="button" onClick={() => setTestCases(old => [...old, { name: `样例 ${old.length + 1}`, input: '', expected: '' }])}>添加样例</button>
          {evaluation && (
            <div className="flow-result">
              <b>{evaluation.passed}/{evaluation.total}</b>
              <span>通过率 {(evaluation.pass_rate * 100).toFixed(0)}%</span>
              <p>{evaluation.summary.recommendation_label}</p>
            </div>
          )}
        </div>
      )
    }

    if (flowPage === 'review') {
      const blocking = validation?.errors || []
      const warnings = validation?.warnings || []
      return (
        <div className="flow-page">
          <div className="flow-page-title row">
            <div>
              <span>REVIEW</span>
              <h2>发布检查</h2>
              <p>检查 manifest、入口文件、权限声明、测试结果和运行资源是否满足发布条件。</p>
            </div>
            <button type="button" className="primary" onClick={runPublishCheck} disabled={!activeDraft}>重新检查</button>
          </div>
          <div className="release-check-grid">
            <div className={activeDraft ? 'pass' : 'fail'}><b>草稿</b><span>{activeDraft ? '已生成' : '未生成'}</span></div>
            <div className={selectedSkills.length || selectedKnowledge.length ? 'pass' : 'warn'}><b>资源</b><span>{selectedSkills.length + selectedKnowledge.length + selectedConnectors.length} 项</span></div>
            <div className={evaluation?.ok ? 'pass' : 'warn'}><b>测试</b><span>{evaluation ? `${evaluation.passed}/${evaluation.total}` : '未测试'}</span></div>
            <div className={validation?.ok ? 'pass' : blocking.length ? 'fail' : 'warn'}><b>Manifest</b><span>{validation?.ok ? '通过' : '待确认'}</span></div>
          </div>
          <div className="release-issues">
            {blocking.map(item => <p className="fail" key={item}>阻断：{item}</p>)}
            {warnings.map(item => <p className="warn" key={item}>提醒：{item}</p>)}
            {validation?.ok && <p className="pass">发布检查通过，可以进入发布确认。</p>}
            {!validation && <p>点击“重新检查”生成发布检查结果。</p>}
          </div>
          <div className="flow-page-actions">
            <button type="button" onClick={() => setFlowPage('test')}>返回测试</button>
            <button type="button" className="primary" onClick={() => setFlowPage('publish')} disabled={!validation?.ok}>进入发布</button>
          </div>
        </div>
      )
    }

    return (
      <div className="flow-page publish-flow-page">
        <div className="flow-page-title">
          <span>PUBLISH</span>
          <h2>发布确认</h2>
          <p>确认上线后，这个 Agent 会进入“我的 Agents”列表，并可通过运行按钮发起真实任务。</p>
        </div>
        <div className="publish-summary">
          <div><b>{projectSummary.name}</b><span>Agent 名称</span></div>
          <div><b>{selectedSkills.length}</b><span>Skills</span></div>
          <div><b>{selectedKnowledge.length}</b><span>知识库</span></div>
          <div><b>{selectedConnectors.length}</b><span>连接器</span></div>
        </div>
        <button
          type="button"
          className="publish-button"
          disabled={!validation?.ok || published}
          onClick={() => {
            setPublished(true)
            notice('发布流程已完成')
          }}
        >
          {published ? '已发布' : '确认发布'}
        </button>
        {published && <button type="button" onClick={onGoToAgentsList}>回到我的 Agents</button>}
      </div>
    )
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
          <span>{activeDraft ? '已创建文件，可在左侧高级编辑' : '描述业务场景即可生成 Agent'}</span>
        </div>
        <section className="left-resource-board">
          <div className="left-resource-title">
            <strong>运行资源</strong>
            <button type="button" onClick={() => setFlowPage('resources')}>配置</button>
          </div>
          {renderResourcePickers(true)}
        </section>
        <section className={`project-inline-editor ${editorOpen ? 'open' : ''}`}>
          <button type="button" className="inline-editor-toggle" onClick={() => setEditorOpen(open => !open)} disabled={!activeDraft}>
            高级编辑
            <span>{activeDraft ? activeDraft.name : '等待草稿'}</span>
          </button>
          {editorOpen && activeDraft && (
            <div className="inline-editor-body">
              <div className="inline-file-list">
                {fileTree.map(file => (
                  <button
                    type="button"
                    className={selectedFile === file.path ? 'active' : ''}
                    key={file.path}
                    onClick={() => openDraftFile(activeDraft.id, file.path)}
                  >
                    {file.name}
                  </button>
                ))}
              </div>
              <textarea value={fileContent} onChange={event => setFileContent(event.target.value)} spellCheck={false} />
              <button type="button" className="inline-save" onClick={saveCurrentFile} disabled={savingFile || !selectedFile}>
                {savingFile ? '保存中' : '保存文件'}
              </button>
            </div>
          )}
        </section>
      </aside>

      <main className="project-chat-main">
        <header className="project-chat-header auto-header">
          <div className="project-avatar">A</div>
          <div>
            <span className="auto-eyebrow">Conversational Agent Builder</span>
            <h1>{projectSummary.name}</h1>
            <p>像聊天一样自动生成 Agent：需求理解、能力设计、文件创建、左侧高级编辑和右侧运行预览一步完成。</p>
          </div>
        </header>

        <div className="auto-progress-strip flow-progress-strip">
          {flowSteps.map(step => (
            <button
              type="button"
              className={flowPage === step.key ? 'active' : ''}
              key={step.key}
              onClick={() => setFlowPage(step.key)}
            >
              <b>{step.title}</b>
              <span>{step.desc}</span>
            </button>
          ))}
        </div>

        {renderFlowMain()}
      </main>

      <aside className="project-settings-pane auto-settings-pane run-console-pane">
        <div className="run-console-head">
          <span>运行预览</span>
          <h2>{activeDraft?.name || generated?.draft.name || '等待 Agent'}</h2>
          <p>这里和“我的 Agents”点击运行后的对话体验保持一致：输入任务、查看步骤、读取输出。</p>
        </div>

        <div className="agent-chat-row assistant compact">
          <span>A</span>
          <div>
            <strong>{activeDraft?.name || 'Atlas Agent'}</strong>
            <p>{generated || activeDraft ? '我已经可以试运行。你可以输入业务问题，我会按当前草稿输出结果。' : '生成或打开一个 Agent 后即可运行预览。'}</p>
          </div>
        </div>

        <label className="agent-use-input compact">
          让 Agent 处理什么任务？
          <textarea
            value={runInput}
            onChange={event => setRunInput(event.target.value)}
            placeholder="例如：请根据知识库说明售后处理流程，并生成一段给客户的回复。"
          />
        </label>

        <button className="run-console-button" type="button" onClick={runActiveDraft} disabled={runBusy || !runInput.trim()}>
          {runBusy ? '运行中' : '运行 Agent'}
        </button>

        <div className="run-mini-steps console-steps">
          <div className={activeDraft ? 'active' : ''}><i>1</i>校验 manifest</div>
          <div className={runBusy ? 'active' : ''}><i>2</i>执行 Agent</div>
          <div className={runLogs && !runBusy ? 'active' : ''}><i>3</i>输出结果</div>
        </div>

        <div className="agent-answer-card console-answer">
          <div className="answer-title">
            <span>Agent 输出</span>
            <b>{runBusy ? '运行中' : '就绪'}</b>
          </div>
          <pre>{runLogs}</pre>
        </div>

        <section className="settings-section">
          <span className="settings-label">生成文件</span>
          <div className="file-chips">
            {projectSummary.files.length ? projectSummary.files.map(file => <b key={file}>{file}</b>) : fileTree.map(file => <b key={file.path}>{file.name}</b>)}
            {!projectSummary.files.length && !fileTree.length && <b>对话后自动生成</b>}
          </div>
        </section>
      </aside>
    </section>
  )
}
