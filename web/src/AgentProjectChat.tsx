import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import AgentEvaluationPanel from './AgentEvaluationPanel'
import {
  configureModelProvider,
  evaluateDraftApp,
  generateAgentApp,
  getDraftFile,
  getDraftPreview,
  getDraftProductState,
  listConnectors,
  listDraftFiles,
  listModels,
  listSkills,
  listWorkspaceMembers,
  publishAgent,
  refineAgentApp,
  runDraftApp,
  saveDraftFile,
  updateDeployConfig,
  validateDraftManifest,
  type AppDraft,
  type AppFile,
  type BuilderMessage,
  type DraftProductState,
  type EvaluationCase,
  type EvaluationResult,
  type ManifestValidation,
  type RunPreviewResult,
} from './api'
import type { Agent, Connector, DeployConfig, KnowledgeBase, ModelCatalog, ModelProvider, Skill, WorkspaceMember } from './types'
import './product-builder.css'

type FlowPage = 'develop' | 'test' | 'evaluate' | 'deploy' | 'publish'
type RightView = 'run' | 'preview' | 'code' | 'logs'
type UiMessage = BuilderMessage & { kind?: 'message' | 'error' | 'config' }

const starterPrompts = [
  '做一个企业知识库问答助手，回答制度问题并展示引用来源',
  '做一个客服助手，根据售后知识库回答问题并生成工单摘要',
  '做一个销售线索助手，总结客户信息并生成下一步跟进计划',
]

const emptyDeployConfig: DeployConfig = {
  visibility: 'workspace',
  shared_user_ids: [],
  allowed_knowledge_base_ids: [],
  allowed_skill_ids: [],
  allowed_connectors: [],
  write_confirm: true,
  api_access: false,
  call_log_enabled: true,
  high_risk_approved: false,
}

const visibilityLabels: Record<DeployConfig['visibility'], string> = {
  private: '仅自己',
  shared: '指定成员',
  workspace: '工作空间',
  marketplace: '组织应用市场',
}

function uid() {
  return crypto.randomUUID()
}

function metricText(value: number | null | undefined, percent = true) {
  if (value == null) return '待配置'
  return percent ? `${Math.round(value * 100)}%` : `${Math.round(value)} ms`
}

export default function AgentProjectChat({
  onGoToAgentsList,
  knowledgeBases,
  notice,
  initialMessage,
  initialDraft,
}: {
  onGoToAgentsList: () => void
  knowledgeBases: KnowledgeBase[]
  notice: (text: string) => void
  initialMessage?: { text: string; nonce: number } | null
  initialDraft?: AppDraft | null
}) {
  const [activeDraft, setActiveDraft] = useState<AppDraft | null>(initialDraft || null)
  const [productState, setProductState] = useState<DraftProductState | null>(null)
  const [projectName, setProjectName] = useState(initialDraft?.name || '新 Agent 项目')
  const [flowPage, setFlowPage] = useState<FlowPage>('develop')
  const [rightView, setRightView] = useState<RightView>('run')
  const [messages, setMessages] = useState<UiMessage[]>([
    {
      id: uid(), role: 'assistant', created_at: new Date().toISOString(),
      content: '描述你要解决的业务问题。我会创建完整 Agent 项目；之后继续对话会修改同一个项目并生成新版本。',
    },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [skills, setSkills] = useState<Skill[]>([])
  const [connectors, setConnectors] = useState<Connector[]>([])
  const [models, setModels] = useState<ModelCatalog | null>(null)
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [selectedKnowledge, setSelectedKnowledge] = useState<string[]>([])
  const [selectedSkills, setSelectedSkills] = useState<string[]>([])
  const [selectedConnectors, setSelectedConnectors] = useState<string[]>([])
  const [selectedModel, setSelectedModel] = useState('qwen-turbo')
  const [savingResources, setSavingResources] = useState(false)
  const [files, setFiles] = useState<AppFile[]>([])
  const [selectedFile, setSelectedFile] = useState('')
  const [fileContent, setFileContent] = useState('')
  const [editorOpen, setEditorOpen] = useState(false)
  const [savingFile, setSavingFile] = useState(false)
  const [previewHtml, setPreviewHtml] = useState('')
  const [runInput, setRunInput] = useState('请介绍你的能力，并说明当前可以使用哪些资源。')
  const [runBusy, setRunBusy] = useState(false)
  const [runResult, setRunResult] = useState<RunPreviewResult | null>(null)
  const [testCases, setTestCases] = useState<EvaluationCase[]>([
    { name: '基础任务', input: '请介绍你的能力', expected: '' },
  ])
  const [evaluation, setEvaluation] = useState<EvaluationResult | null>(null)
  const [evaluating, setEvaluating] = useState(false)
  const [evaluationSuiteOpen, setEvaluationSuiteOpen] = useState(false)
  const [validation, setValidation] = useState<ManifestValidation | null>(null)
  const [deployConfig, setDeployConfig] = useState<DeployConfig>(emptyDeployConfig)
  const [savingDeploy, setSavingDeploy] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [configProvider, setConfigProvider] = useState<ModelProvider | null>(null)
  const [apiKeyDraft, setApiKeyDraft] = useState('')
  const [baseUrlDraft, setBaseUrlDraft] = useState('')
  const [configBusy, setConfigBusy] = useState(false)
  const [pendingRun, setPendingRun] = useState('')
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  const projectSummary = useMemo(() => ({
    name: activeDraft?.name || projectName,
    version: productState?.agent.version_no || 0,
    publishedVersion: productState?.agent.published_version_no,
    dirty: productState?.draft_dirty || false,
  }), [activeDraft, projectName, productState])

  useEffect(() => {
    void Promise.all([listSkills(), listConnectors(), listModels(), listWorkspaceMembers()])
      .then(([skillItems, connectorItems, catalog, memberItems]) => {
        setSkills(skillItems)
        setConnectors(connectorItems)
        setModels(catalog)
        setMembers(memberItems)
      })
      .catch(() => notice('资源目录加载失败'))
  }, [])

  useEffect(() => {
    if (initialMessage) setInput(initialMessage.text)
  }, [initialMessage?.nonce])

  useEffect(() => {
    if (initialDraft?.id) void openProject(initialDraft)
  }, [initialDraft?.id])

  useEffect(() => {
    messagesRef.current?.scrollTo({ top: messagesRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, busy])

  function appendMessage(role: UiMessage['role'], content: string, extra: Partial<UiMessage> = {}) {
    setMessages(old => [...old, { id: uid(), role, content, created_at: new Date().toISOString(), ...extra }])
  }

  function parseManifest(content: string) {
    try {
      return JSON.parse(content || '{}') as Record<string, unknown>
    } catch {
      return {}
    }
  }

  function hydrateManifest(manifest: Record<string, unknown>) {
    setSelectedKnowledge(Array.isArray(manifest.knowledge_bases) ? manifest.knowledge_bases.filter((item): item is string => typeof item === 'string') : [])
    setSelectedSkills(Array.isArray(manifest.skills) ? manifest.skills.filter((item): item is string => typeof item === 'string') : [])
    setSelectedConnectors(Array.isArray(manifest.connectors) ? manifest.connectors.filter((item): item is string => typeof item === 'string') : [])
    if (typeof manifest.model === 'string') setSelectedModel(manifest.model)
  }

  async function loadFiles(draftId: string) {
    const tree = await listDraftFiles(draftId)
    setFiles(tree)
    const manifestFile = await getDraftFile(draftId, 'manifest.json')
    hydrateManifest(parseManifest(manifestFile.content))
    const nextPath = selectedFile && tree.some(item => item.path === selectedFile)
      ? selectedFile
      : tree.find(item => item.path === 'manifest.json')?.path || tree[0]?.path || ''
    if (nextPath) await openFile(draftId, nextPath)
  }

  async function loadPreview(draftId: string) {
    const preview = await getDraftPreview(draftId)
    setPreviewHtml(preview.html)
  }

  async function refreshState(draftId: string, restoreMessages = false) {
    const state = await getDraftProductState(draftId)
    setProductState(state)
    setDeployConfig(state.deploy_config)
    if (state.latest_evaluation) {
      setEvaluation({
        ok: state.latest_evaluation.ok,
        passed: state.latest_evaluation.passed,
        total: state.latest_evaluation.total,
        pass_rate: state.latest_evaluation.pass_rate,
        results: state.latest_evaluation.results,
        summary: state.latest_evaluation.summary,
      })
    }
    if (restoreMessages && state.builder_messages.length) {
      setMessages(state.builder_messages.map(item => ({ ...item, kind: 'message' })))
    }
    return state
  }

  async function openProject(draft: AppDraft) {
    setActiveDraft(draft)
    setProjectName(draft.name)
    setEditorOpen(true)
    try {
      await Promise.all([loadFiles(draft.id), loadPreview(draft.id), refreshState(draft.id, true)])
    } catch (error) {
      notice(error instanceof Error ? error.message : '项目加载失败')
    }
  }

  async function openFile(draftId: string, path: string) {
    try {
      const result = await getDraftFile(draftId, path)
      setSelectedFile(path)
      setFileContent(result.content)
      if (path === 'manifest.json') hydrateManifest(parseManifest(result.content))
    } catch {
      notice('文件读取失败')
    }
  }

  async function saveCurrentFile() {
    if (!activeDraft || !selectedFile) return
    setSavingFile(true)
    try {
      await saveDraftFile(activeDraft.id, selectedFile, fileContent)
      if (selectedFile === 'manifest.json') hydrateManifest(parseManifest(fileContent))
      await Promise.all([refreshState(activeDraft.id), loadPreview(activeDraft.id)])
      notice(`${selectedFile} 已保存`)
    } catch (error) {
      notice(error instanceof Error ? error.message : '文件保存失败')
    } finally {
      setSavingFile(false)
    }
  }

  async function readManifest() {
    if (!activeDraft) return {}
    if (selectedFile === 'manifest.json') return parseManifest(fileContent)
    return parseManifest((await getDraftFile(activeDraft.id, 'manifest.json')).content)
  }

  async function saveResourceConfig(showNotice = true) {
    if (!activeDraft) {
      if (showNotice) notice('请先创建 Agent 项目')
      return null
    }
    setSavingResources(true)
    try {
      const manifest = await readManifest()
      const next = {
        ...manifest,
        model: selectedModel,
        knowledge_bases: selectedKnowledge,
        skills: selectedSkills,
        connectors: selectedConnectors,
      }
      const content = JSON.stringify(next, null, 2)
      await saveDraftFile(activeDraft.id, 'manifest.json', content)
      if (selectedFile === 'manifest.json') setFileContent(content)
      const check = await validateDraftManifest(activeDraft.id)
      setValidation(check)
      await refreshState(activeDraft.id)
      if (showNotice) notice('运行资源已保存')
      return check
    } catch (error) {
      if (showNotice) notice(error instanceof Error ? error.message : '资源保存失败')
      return null
    } finally {
      setSavingResources(false)
    }
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    const content = input.trim()
    if (!content || busy) return
    setInput('')
    appendMessage('user', content)
    setBusy(true)
    try {
      if (activeDraft) {
        const result = await refineAgentApp(activeDraft.id, content)
        setActiveDraft(result.draft)
        setProjectName(result.draft.name)
        appendMessage('assistant', result.reply, { changed_files: result.changed_files, version_no: result.version_no })
        await Promise.all([loadFiles(activeDraft.id), loadPreview(activeDraft.id), refreshState(activeDraft.id)])
      } else {
        const result = await generateAgentApp(content, '')
        setActiveDraft(result.draft)
        setProjectName(result.draft.name)
        setRunInput(result.blueprint.sample_input || content)
        setTestCases([{ name: '生成样例', input: result.blueprint.sample_input || content, expected: '' }])
        await Promise.all([loadFiles(result.draft.id), loadPreview(result.draft.id), refreshState(result.draft.id, true)])
      }
      setEditorOpen(true)
      notice(activeDraft ? 'Agent 已更新并生成新版本' : 'Agent 项目已创建')
    } catch (error) {
      appendMessage('assistant', error instanceof Error ? error.message : 'Coding Agent 执行失败', { kind: 'error' })
    } finally {
      setBusy(false)
    }
  }

  function providerForModel() {
    return models?.providers.find(provider => provider.models.includes(selectedModel)) || null
  }

  function requireProvider(inputText: string) {
    const provider = providerForModel()
    if (!provider || provider.configured) return false
    setConfigProvider(provider)
    setBaseUrlDraft(provider.base_url || '')
    setPendingRun(inputText)
    appendMessage(
      'assistant',
      `当前 Agent 使用 ${selectedModel}，需要先配置 ${provider.name} API Key 才能真实运行。你可以在下面保存 Key，我会继续刚才的任务；也可以先使用本地代码回退。`,
      { kind: 'config' },
    )
    setFlowPage('develop')
    return true
  }

  async function saveProviderAndContinue() {
    if (!configProvider || !apiKeyDraft.trim()) return notice('请输入 API Key')
    setConfigBusy(true)
    try {
      const catalog = await configureModelProvider(configProvider.id, apiKeyDraft.trim(), baseUrlDraft.trim())
      setModels(catalog)
      setApiKeyDraft('')
      setConfigProvider(null)
      appendMessage('assistant', `${configProvider.name} 已配置，继续运行刚才的任务。`)
      const task = pendingRun
      setPendingRun('')
      if (task) await runAgent(task, [], true)
    } catch (error) {
      notice(error instanceof Error ? error.message : '模型配置失败')
    } finally {
      setConfigBusy(false)
    }
  }

  async function runAgent(overrideInput?: string, confirmedTools: string[] = [], skipProviderCheck = false) {
    if (!activeDraft || runBusy) return
    const task = (overrideInput ?? runInput).trim()
    if (!task) return
    if (!skipProviderCheck && requireProvider(task)) return
    setRunBusy(true)
    setRightView('run')
    try {
      if (selectedFile) await saveCurrentFile()
      await saveResourceConfig(false)
      const result = await runDraftApp(activeDraft.id, task, confirmedTools)
      setRunResult(result)
      await refreshState(activeDraft.id)
      notice(result.ok ? 'Agent 运行完成' : result.error === 'confirmation_required' ? '需要确认写操作' : 'Agent 运行失败')
    } catch (error) {
      setRunResult({
        ok: false, answer: '', logs: error instanceof Error ? error.message : '运行失败',
        error: error instanceof Error ? error.message : '运行失败', sources: [], trace: [], tool_calls: [],
      })
    } finally {
      setRunBusy(false)
    }
  }

  async function runEvaluation() {
    if (!activeDraft) return
    setEvaluating(true)
    try {
      await saveResourceConfig(false)
      const result = await evaluateDraftApp(activeDraft.id, testCases)
      setEvaluation(result)
      await refreshState(activeDraft.id)
      notice(`评测完成：${result.passed}/${result.total} 通过`)
    } catch (error) {
      notice(error instanceof Error ? error.message : '评测失败')
    } finally {
      setEvaluating(false)
    }
  }

  function toggleResource(value: string, list: string[], setList: (next: string[]) => void) {
    setList(list.includes(value) ? list.filter(item => item !== value) : [...list, value])
  }

  function updateDeploy<K extends keyof DeployConfig>(key: K, value: DeployConfig[K]) {
    setDeployConfig(old => ({ ...old, [key]: value }))
  }

  function toggleDeployList(key: 'shared_user_ids' | 'allowed_knowledge_base_ids' | 'allowed_skill_ids' | 'allowed_connectors', value: string) {
    const list = deployConfig[key]
    updateDeploy(key, list.includes(value) ? list.filter(item => item !== value) : [...list, value])
  }

  async function saveDeploy() {
    if (!activeDraft) return
    setSavingDeploy(true)
    try {
      await updateDeployConfig(activeDraft.id, deployConfig)
      await refreshState(activeDraft.id)
      notice('落地配置已保存')
    } catch (error) {
      notice(error instanceof Error ? error.message : '落地配置保存失败')
    } finally {
      setSavingDeploy(false)
    }
  }

  async function refreshPublishCheck() {
    if (!activeDraft) return
    await saveResourceConfig(false)
    const check = await validateDraftManifest(activeDraft.id)
    setValidation(check)
    await refreshState(activeDraft.id)
    notice(check.ok ? '项目文件校验通过' : '项目文件存在阻断项')
  }

  async function publish() {
    if (!activeDraft) return
    setPublishing(true)
    try {
      await publishAgent(activeDraft.id)
      await refreshState(activeDraft.id)
      notice(`${activeDraft.name} 已发布`)
    } catch (error) {
      notice(error instanceof Error ? error.message : '发布失败')
    } finally {
      setPublishing(false)
    }
  }

  function renderConfigCard() {
    if (!configProvider) return null
    return (
      <div className="agent-config-card">
        <div className="config-card-head"><span>运行依赖</span><strong>{configProvider.name} API Key</strong></div>
        <p>Key 只用于模型调用。保存后会继续刚才的运行任务。</p>
        <label><span>Base URL</span><input value={baseUrlDraft} onChange={event => setBaseUrlDraft(event.target.value)} /></label>
        <label><span>API Key</span><input type="password" value={apiKeyDraft} onChange={event => setApiKeyDraft(event.target.value)} placeholder="sk-..." /></label>
        <div className="config-card-actions">
          <button className="primary" type="button" onClick={saveProviderAndContinue} disabled={configBusy || !apiKeyDraft.trim()}>{configBusy ? '保存中' : '保存并继续'}</button>
          <button type="button" onClick={async () => {
            const task = pendingRun
            setConfigProvider(null)
            setPendingRun('')
            appendMessage('assistant', '已切换为本地代码回退。此模式用于结构验证，不代表真实模型回答质量。')
            if (task) await runAgent(task, [], true)
          }}>使用本地回退</button>
        </div>
      </div>
    )
  }

  function renderDevelop() {
    return (
      <div className="develop-workspace">
        <div className="project-messages" ref={messagesRef}>
          {messages.map(message => (
            <div className={`project-message ${message.role}`} key={message.id}>
              {message.role === 'assistant' && <span className="bot-avatar">A</span>}
              <div className={message.kind === 'error' ? 'agent-failure-card' : 'builder-message-body'}>
                <p>{message.content}</p>
                {(message.version_no || message.changed_files?.length) && (
                  <div className="change-summary">
                    {message.version_no && <b>v{message.version_no}</b>}
                    {message.changed_files?.map(file => <span key={file}>{file}</span>)}
                  </div>
                )}
                {message.kind === 'config' && renderConfigCard()}
              </div>
            </div>
          ))}
          {busy && <div className="project-message assistant"><span className="bot-avatar">A</span><div className="agent-thinking"><i className="typing-dot" />Coding Agent 正在分析并修改项目...</div></div>}
        </div>
        <form className="project-composer auto-composer" onSubmit={submit}>
          {!activeDraft && <div className="starter-row">{starterPrompts.map(item => <button type="button" key={item} onClick={() => setInput(item)}>{item}</button>)}</div>}
          <textarea
            ref={inputRef}
            value={input}
            onChange={event => setInput(event.target.value)}
            placeholder={activeDraft ? '继续描述要修改的功能、规则、文件或输出格式...' : '描述要创建的 Agent，例如：做一个客服助手，根据知识库回答售后问题...'}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                event.currentTarget.form?.requestSubmit()
              }
            }}
          />
          <div className="project-composer-actions">
            <span>{activeDraft ? `下一次修改将生成 v${projectSummary.version + 1}` : '首次对话将创建完整项目'}</span>
            <button className="send-project" disabled={busy || !input.trim()}>{busy ? '...' : '↑'}</button>
          </div>
        </form>
      </div>
    )
  }

  function renderRunEvidence(result: RunPreviewResult | null) {
    if (!result) return <div className="flow-empty">运行后将在这里展示回答、来源、Skill 和工具步骤。</div>
    return (
      <div className="runtime-evidence">
        <section className="runtime-answer"><div><b>Agent 回答</b><span>{result.ok ? '运行成功' : '需要处理'}</span></div><p>{result.answer || result.error || '无输出'}</p></section>
        {!result.ok && result.error !== 'confirmation_required' && (
          <div className="repair-action">
            <span>把运行错误和日志交回 Coding Agent，修改会生成新版本。</span>
            <button type="button" onClick={() => {
              setInput(`请修复当前 Agent 的运行问题。错误：${result.error || '未知'}\n日志：${result.logs.slice(0, 3000)}`)
              setFlowPage('develop')
              window.setTimeout(() => inputRef.current?.focus(), 0)
            }}>交给 Coding Agent 修复</button>
          </div>
        )}
        {result.requires_confirmation && (
          <section className="runtime-confirm">
            <b>写操作确认</b>
            <p>Agent 准备执行 {result.requires_confirmation.tool}。确认后会用相同输入重新运行并放行该工具。</p>
            <button type="button" className="primary" onClick={() => runAgent(runInput, [result.requires_confirmation!.tool], true)}>确认执行</button>
          </section>
        )}
        <div className="runtime-columns">
          <section><h3>执行步骤</h3>{result.trace.map((step, index) => <div className={`runtime-step ${step.status}`} key={`${step.title}-${index}`}><i>{index + 1}</i><span><b>{step.title}</b><small>{step.executor} · {step.status}</small></span></div>)}{!result.trace.length && <p>没有步骤记录</p>}</section>
          <section><h3>引用来源</h3>{result.sources.map((source, index) => <div className="runtime-source" key={`${source.document}-${index}`}><b>{source.document} · 第 {source.page} 页</b><p>{source.quote}</p></div>)}{!result.sources.length && <p>本次未使用知识库来源</p>}</section>
          <section><h3>工具调用</h3>{result.tool_calls.map((tool, index) => <div className="runtime-tool" key={`${tool.name}-${index}`}><b>{tool.name}</b><span>{tool.provider || 'platform'} · {tool.status}</span></div>)}{!result.tool_calls.length && <p>本次未调用外部工具</p>}</section>
        </div>
      </div>
    )
  }

  function renderTest() {
    return (
      <div className="flow-page test-workspace">
        <div className="flow-page-title row"><div><span>TEST</span><h2>单次测试</h2><p>验证当前草稿能否运行，并查看知识库引用、Skill 和连接器步骤。</p></div><b className="evidence-badge">草稿 v{projectSummary.version}{projectSummary.dirty ? ' · 有未保存版本的修改' : ''}</b></div>
        <div className="single-test-input"><textarea value={runInput} onChange={event => setRunInput(event.target.value)} /><button type="button" className="primary" onClick={() => runAgent()} disabled={!activeDraft || runBusy}>{runBusy ? '运行中' : '运行测试'}</button></div>
        {renderRunEvidence(runResult)}
      </div>
    )
  }

  function renderEvaluate() {
    const metrics = evaluation?.summary.metrics || {}
    const failures = evaluation?.results.filter(item => !item.ok) || []
    const releaseGate = productState?.publish_checklist.items.find(item => item.key === 'release_evaluation')
    return (
      <div className="flow-page evaluation-workspace">
        <div className="flow-page-title row"><div><span>EVALUATE</span><h2>批量评测</h2><p>用固定评测集判断发布风险。语义指标缺少依据时会明确标记为待配置。</p></div><div className="flow-page-actions"><button type="button" onClick={() => setEvaluationSuiteOpen(true)} disabled={!activeDraft}>评测集与发布门禁</button><button type="button" className="primary" onClick={runEvaluation} disabled={!activeDraft || evaluating || !testCases.length}>{evaluating ? '评测中' : '运行全部评测'}</button></div></div>
        <section className="deploy-band evaluation-suite-summary"><div><h3>可复用评测集</h3><p>为当前版本保存回归样例，配置关键词、JSON Schema、时延和 LLM Judge 评分。标记为发布门禁的评测集必须在当前版本通过后才能发布。</p></div><div className={releaseGate?.ok ? 'evaluation-gate-status pass' : 'evaluation-gate-status warn'}>{releaseGate ? `${releaseGate.ok ? '发布门禁已通过' : '发布门禁未通过'}：${releaseGate.label}` : '尚未配置发布评测集'}</div></section>
        <div className="test-case-table">
          {testCases.map((item, index) => (
            <div className="test-case-row" key={index}>
              <input value={item.name} onChange={event => setTestCases(old => old.map((row, i) => i === index ? { ...row, name: event.target.value } : row))} aria-label="样例名称" />
              <textarea value={item.input} onChange={event => setTestCases(old => old.map((row, i) => i === index ? { ...row, input: event.target.value } : row))} aria-label="测试输入" />
              <textarea value={item.expected} onChange={event => setTestCases(old => old.map((row, i) => i === index ? { ...row, expected: event.target.value } : row))} aria-label="期望包含" placeholder="期望输出包含，可留空" />
              <button type="button" onClick={() => setTestCases(old => old.filter((_, i) => i !== index))}>删除</button>
            </div>
          ))}
        </div>
        <button type="button" onClick={() => setTestCases(old => [...old, { name: `样例 ${old.length + 1}`, input: '', expected: '' }])}>添加样例</button>
        {evaluation && (
          <>
            <div className="evaluation-verdict"><b>{evaluation.summary.recommendation_label}</b><span>{evaluation.passed}/{evaluation.total} 通过 · 平均 {evaluation.summary.avg_elapsed_ms.toFixed(0)} ms</span></div>
            <div className="metric-grid">
              <div><b>{metricText(metrics.task_completion_rate)}</b><span>任务完成率</span></div>
              <div><b>{metricText(metrics.first_pass_rate)}</b><span>一次解决率</span></div>
              <div><b>{metricText(metrics.skill_success_rate)}</b><span>Skill 成功率</span></div>
              <div><b>{metricText(metrics.tool_success_rate)}</b><span>工具成功率</span></div>
              <div><b>{metricText(metrics.citation_accuracy)}</b><span>引用准确率</span></div>
              <div><b>{metricText(metrics.hallucination_rate)}</b><span>幻觉率</span></div>
              <div><b>{metricText(metrics.avg_elapsed_ms, false)}</b><span>平均响应</span></div>
              <div><b>{metrics.token_cost == null ? '待计量' : metrics.token_cost}</b><span>任务成本</span></div>
            </div>
            <section className="failure-samples"><h3>失败样例</h3>{failures.map((item, index) => <div key={`${item.name}-${index}`}><b>{item.name}</b><p>期望：{item.expected || '完成任务'}</p><p>实际：{item.output || item.logs}</p><span>{item.suggestion}</span><button type="button" onClick={() => { setInput(`请修复评测失败样例“${item.name}”。输入：${item.input}\n期望：${item.expected || '完成任务'}\n实际：${item.output || item.logs.slice(0, 1800)}\n建议：${item.suggestion}`); setFlowPage('develop'); window.setTimeout(() => inputRef.current?.focus(), 0) }}>生成修复版本</button></div>)}{!failures.length && <p>当前评测集没有失败样例。</p>}</section>
          </>
        )}
      </div>
    )
  }

  function renderDeploy() {
    return (
      <div className="flow-page deploy-workspace">
        <div className="flow-page-title row"><div><span>DEPLOY</span><h2>落地配置</h2><p>确定谁可以使用、允许访问哪些资源，以及外部调用和审计规则。</p></div><button className="primary" type="button" onClick={saveDeploy} disabled={!activeDraft || savingDeploy}>{savingDeploy ? '保存中' : '保存配置'}</button></div>
        <section className="deploy-band"><h3>可见范围</h3><div className="deploy-choice-grid">{(Object.keys(visibilityLabels) as DeployConfig['visibility'][]).map(item => <label className={deployConfig.visibility === item ? 'selected' : ''} key={item}><input type="radio" checked={deployConfig.visibility === item} onChange={() => updateDeploy('visibility', item)} />{visibilityLabels[item]}</label>)}</div>{deployConfig.visibility === 'shared' && <div className="deploy-inline-list">{members.map(member => <label key={member.id}><input type="checkbox" checked={deployConfig.shared_user_ids.includes(member.id)} onChange={() => toggleDeployList('shared_user_ids', member.id)} />{member.name}</label>)}</div>}</section>
        <section className="deploy-band"><h3>发布资源权限 <small>留空表示仅按开发阶段绑定资源执行</small></h3><div className="deploy-permission-columns"><div><b>知识库</b>{knowledgeBases.map(kb => <label key={kb.id}><input type="checkbox" checked={deployConfig.allowed_knowledge_base_ids.includes(kb.id)} onChange={() => toggleDeployList('allowed_knowledge_base_ids', kb.id)} />{kb.name}</label>)}</div><div><b>Skills</b>{skills.map(skill => <label key={skill.id}><input type="checkbox" checked={deployConfig.allowed_skill_ids.includes(skill.id)} onChange={() => toggleDeployList('allowed_skill_ids', skill.id)} />{skill.name}</label>)}</div><div><b>连接器</b>{connectors.map(connector => <label key={connector.provider}><input type="checkbox" checked={deployConfig.allowed_connectors.includes(connector.provider)} onChange={() => toggleDeployList('allowed_connectors', connector.provider)} />{connector.name}</label>)}</div></div></section>
        <section className="deploy-band deploy-switches"><h3>运行治理</h3><label><input type="checkbox" checked={deployConfig.write_confirm} onChange={event => updateDeploy('write_confirm', event.target.checked)} /><span><b>写操作二次确认</b><small>发送消息、修改外部数据前要求用户确认</small></span></label><label><input type="checkbox" checked={deployConfig.api_access} onChange={event => updateDeploy('api_access', event.target.checked)} /><span><b>允许外部 API 调用</b><small>发布后可以创建 API Key</small></span></label><label><input type="checkbox" checked={deployConfig.call_log_enabled} onChange={event => updateDeploy('call_log_enabled', event.target.checked)} /><span><b>保留调用日志</b><small>记录来源、状态、耗时和错误</small></span></label><label className="high-risk-approval"><input type="checkbox" checked={deployConfig.high_risk_approved} onChange={event => updateDeploy('high_risk_approved', event.target.checked)} /><span><b>管理员批准高风险连接器</b><small>仅工作空间所有者可保存；适用于发送消息和修改外部数据的连接器</small></span></label></section>
      </div>
    )
  }

  function renderPublish() {
    const checklist = productState?.publish_checklist
    const published = productState?.agent.status === 'published' && !productState.agent.has_unpublished_changes
    return (
      <div className="flow-page publish-workspace">
        <div className="flow-page-title row"><div><span>PUBLISH</span><h2>发布版本</h2><p>发布会自动保存当前工作区，并生成不可变发布版本。评测可以跳过，但会保留风险提醒。</p></div><button type="button" onClick={refreshPublishCheck} disabled={!activeDraft}>重新检查</button></div>
        <div className="publish-version-strip"><div><small>当前草稿</small><b>v{projectSummary.version}{projectSummary.dirty ? ' + 未版本化修改' : ''}</b></div><div><small>线上版本</small><b>{projectSummary.publishedVersion ? `v${projectSummary.publishedVersion}` : '尚未发布'}</b></div><div><small>目标范围</small><b>{visibilityLabels[deployConfig.visibility]}</b></div></div>
        <section className="publish-checklist"><h3>发布检查</h3>{checklist?.items.map(item => <div className={item.ok ? 'pass' : item.level === 'blocking' ? 'fail' : 'warn'} key={item.key}><i>{item.ok ? '✓' : item.level === 'blocking' ? '×' : '!'}</i><span><b>{item.label}</b><small>{item.level === 'blocking' ? '必须通过' : '风险提醒'}</small></span></div>)}{validation?.errors.map(error => <p className="fail" key={error}>{error}</p>)}</section>
        {published ? <div className="publish-success"><b>已发布 v{projectSummary.publishedVersion}</b><p>工作台和外部 API 会固定运行这个版本，后续草稿修改不会影响线上。</p><button type="button" onClick={onGoToAgentsList}>回到我的 Agents</button></div> : <button className="publish-button" type="button" onClick={publish} disabled={!checklist?.can_publish || publishing}>{publishing ? '发布中' : productState?.agent.status === 'published' ? '发布草稿更新' : '确认发布当前版本'}</button>}
      </div>
    )
  }

  function renderMain() {
    if (flowPage === 'develop') return renderDevelop()
    if (flowPage === 'test') return renderTest()
    if (flowPage === 'evaluate') return renderEvaluate()
    if (flowPage === 'deploy') return renderDeploy()
    return renderPublish()
  }

  function renderResourcePanel() {
    return (
      <div className="resource-picker compact product-resource-picker">
        <label className="resource-model"><span>运行模型</span><select value={selectedModel} onChange={event => setSelectedModel(event.target.value)}>{models?.providers.flatMap(provider => provider.models).map(model => <option key={model}>{model}</option>) || <option>{selectedModel}</option>}</select></label>
        <section><div className="resource-picker-head"><b>Skills</b><span>{selectedSkills.length}</span></div><div className="resource-options">{selectedSkills.filter(name => !skills.some(skill => skill.name === name)).map(name => <label className="checked project-skill-option" key={name}><input type="checkbox" checked onChange={() => toggleResource(name, selectedSkills, setSelectedSkills)} /><span><strong>{name}</strong><small>项目内置 Skill</small></span></label>)}{skills.map(skill => <label className={selectedSkills.includes(skill.name) ? 'checked' : ''} key={skill.id}><input type="checkbox" checked={selectedSkills.includes(skill.name)} onChange={() => toggleResource(skill.name, selectedSkills, setSelectedSkills)} /><span><strong>{skill.name}</strong><small>{skill.description}</small></span></label>)}</div></section>
        <section><div className="resource-picker-head"><b>知识库</b><span>{selectedKnowledge.length}</span></div><div className="resource-options">{knowledgeBases.map(kb => <label className={selectedKnowledge.includes(kb.id) ? 'checked' : ''} key={kb.id}><input type="checkbox" checked={selectedKnowledge.includes(kb.id)} onChange={() => toggleResource(kb.id, selectedKnowledge, setSelectedKnowledge)} /><span><strong>{kb.name}</strong><small>{kb.documents.length} 个文档</small></span></label>)}</div></section>
        <section><div className="resource-picker-head"><b>连接器</b><span>{selectedConnectors.length}</span></div><div className="resource-options">{connectors.map(connector => <label className={selectedConnectors.includes(connector.provider) ? 'checked' : ''} key={connector.provider}><input type="checkbox" checked={selectedConnectors.includes(connector.provider)} onChange={() => toggleResource(connector.provider, selectedConnectors, setSelectedConnectors)} /><span><strong>{connector.name}</strong><small>{connector.connected ? '已连接' : connector.configured ? '需要重连' : '未配置'}</small></span></label>)}</div></section>
        <button className="resource-save" type="button" onClick={() => saveResourceConfig()} disabled={!activeDraft || savingResources}>{savingResources ? '保存中' : '保存运行资源'}</button>
      </div>
    )
  }

  const flowSteps: Array<{ key: FlowPage; label: string; detail: string; state: string }> = [
    { key: 'develop', label: '开发', detail: activeDraft ? `v${projectSummary.version}${projectSummary.dirty ? ' · 未保存版本' : ''}` : '描述需求', state: activeDraft ? 'done' : 'todo' },
    { key: 'test', label: '测试', detail: productState?.latest_test ? (productState.latest_test.ok ? '已通过' : '有问题') : '未开始', state: productState?.latest_test?.ok ? 'done' : productState?.latest_test ? 'warn' : 'todo' },
    { key: 'evaluate', label: '评测', detail: productState?.latest_evaluation ? `${Math.round(productState.latest_evaluation.pass_rate * 100)}%` : '可跳过', state: productState?.latest_evaluation?.ok ? 'done' : productState?.latest_evaluation ? 'warn' : 'todo' },
    { key: 'deploy', label: '落地配置', detail: productState?.agent.deploy_config_configured ? '已保存' : '未配置', state: productState?.agent.deploy_config_configured ? 'done' : 'todo' },
    { key: 'publish', label: '发布', detail: productState?.agent.workflow_stage === 'published' ? `v${projectSummary.publishedVersion}` : productState?.agent.status === 'published' ? '有待发布更新' : '未发布', state: productState?.agent.workflow_stage === 'published' ? 'done' : 'todo' },
  ]

  return (
    <section className="project-chat-page product-builder-page">
      <aside className="project-list-pane product-left-rail">
        <div className="project-list-title"><strong>Agent 项目</strong><button type="button" onClick={() => { setActiveDraft(null); setProductState(null); setMessages([]); setProjectName('新 Agent 项目'); setFlowPage('develop') }}>+</button></div>
        <div className="project-user-card auto-card"><span>{productState?.agent.workflow_stage || 'DRAFT'}</span><b>{projectSummary.name}</b><small>{projectSummary.publishedVersion ? `线上 v${projectSummary.publishedVersion}` : activeDraft ? `草稿 v${projectSummary.version}` : '等待创建'}</small></div>
        <section className="left-resource-board"><div className="left-resource-title"><strong>运行资源</strong><span>{selectedKnowledge.length + selectedSkills.length + selectedConnectors.length}</span></div>{renderResourcePanel()}</section>
        <section className={`project-inline-editor ${editorOpen ? 'open' : ''}`}>
          <button className="inline-editor-toggle" type="button" onClick={() => setEditorOpen(open => !open)} disabled={!activeDraft}><span>高级编辑</span><small>{projectSummary.dirty ? '工作区有修改' : '版本已同步'}</small></button>
          {editorOpen && activeDraft && <div className="inline-editor-body"><div className="inline-file-list">{files.map(file => <button type="button" className={selectedFile === file.path ? 'active' : ''} onClick={() => openFile(activeDraft.id, file.path)} key={file.path}>{file.name}</button>)}</div><textarea value={fileContent} onChange={event => setFileContent(event.target.value)} spellCheck={false} /><button className="inline-save" type="button" onClick={saveCurrentFile} disabled={savingFile}>{savingFile ? '保存中' : '保存文件'}</button></div>}
        </section>
      </aside>

      <main className="project-chat-main product-builder-main">
        <header className="project-chat-header product-builder-header"><div className="project-avatar">A</div><div><span className="auto-eyebrow">ATLAS AGENT RELEASE CONSOLE</span><h1>{projectSummary.name}</h1><p>持续对话开发、真实运行证据、批量评测、权限配置和不可变版本发布。</p></div><div className={`builder-health ${productState?.agent.health_status || 'unknown'}`}><small>健康状态</small><b>{productState?.agent.health_status === 'healthy' ? '健康' : productState?.agent.health_status === 'risk' ? '有风险' : productState?.agent.health_status === 'attention' ? '需关注' : '待验证'}</b></div></header>
        <nav className="release-rail">{flowSteps.map((step, index) => <button type="button" className={`${flowPage === step.key ? 'active' : ''} ${step.state}`} key={step.key} onClick={() => setFlowPage(step.key)}><i>{index + 1}</i><span><b>{step.label}</b><small>{step.detail}</small></span></button>)}</nav>
        {renderMain()}
      </main>

      <aside className="project-settings-pane product-runtime-pane">
        <nav className="runtime-tabs">
          <button className={rightView === 'run' ? 'active' : ''} onClick={() => setRightView('run')}>运行</button>
          <button className={rightView === 'preview' ? 'active' : ''} onClick={() => setRightView('preview')} disabled={!previewHtml}>预览</button>
          <button className={rightView === 'code' ? 'active' : ''} onClick={() => setRightView('code')}>代码</button>
          <button className={rightView === 'logs' ? 'active' : ''} onClick={() => setRightView('logs')}>日志</button>
        </nav>
        {rightView === 'run' && <div className="runtime-run-view"><div className="run-console-head"><span>{productState?.agent.status === 'published' ? `草稿预览 · 线上 v${projectSummary.publishedVersion}` : '草稿运行'}</span><h2>{projectSummary.name}</h2><p>开发侧始终运行当前草稿；发布后的工作台与 API 固定运行线上版本。</p></div><div className="runtime-chat-answer"><span>A</span><div><strong>{projectSummary.name}</strong><p>{runResult?.answer || (activeDraft ? '输入任务即可验证当前草稿。' : '创建 Agent 后开始运行。')}</p></div></div><label className="agent-use-input compact"><span>任务输入</span><textarea value={runInput} onChange={event => setRunInput(event.target.value)} /></label><button className="run-console-button" type="button" onClick={() => runAgent()} disabled={!activeDraft || runBusy || !runInput.trim()}>{runBusy ? '运行中' : '运行 Agent'}</button>{runResult?.requires_confirmation && <button className="runtime-confirm-button" type="button" onClick={() => runAgent(runInput, [runResult.requires_confirmation!.tool], true)}>确认 {runResult.requires_confirmation.tool}</button>}<div className="runtime-mini-meta"><span>{runResult?.version_label || 'draft'} {runResult?.version_no ? `v${runResult.version_no}` : 'workspace'}</span><span>{runResult?.elapsed_ms != null ? `${runResult.elapsed_ms} ms` : '未运行'}</span><span>{runResult?.sources.length || 0} 来源</span></div></div>}
        {rightView === 'preview' && <div className="runtime-preview-view">{previewHtml ? <iframe title="Agent HTML 预览" srcDoc={previewHtml} sandbox="allow-forms allow-scripts" referrerPolicy="no-referrer" /> : <div className="flow-empty">项目没有 preview.html。通过开发对话要求 Coding Agent 创建前端页面后会自动显示。</div>}</div>}
        {rightView === 'code' && <div className="runtime-code-view"><div className="runtime-file-index">{files.map(file => <button className={selectedFile === file.path ? 'active' : ''} type="button" key={file.path} onClick={() => activeDraft && openFile(activeDraft.id, file.path)}>{file.path}</button>)}</div><pre>{fileContent || '选择文件查看代码'}</pre></div>}
        {rightView === 'logs' && <div className="runtime-log-view"><div className="runtime-log-status"><b>{runResult?.ok ? 'SUCCEEDED' : runResult ? 'REQUIRES ACTION' : 'IDLE'}</b><span>{runResult?.run_id || '尚无运行记录'}</span></div><pre>{runResult?.logs || '运行后显示版本、模型、资源命中、工具和耗时日志。'}</pre>{runResult?.warnings?.map(warning => <p key={warning}>{warning}</p>)}</div>}
      </aside>
      {evaluationSuiteOpen && activeDraft && <AgentEvaluationPanel agent={productState?.agent || ({ id: activeDraft.id, name: activeDraft.name } as Agent)} onClose={() => { setEvaluationSuiteOpen(false); void refreshState(activeDraft.id) }} notice={notice} />}
    </section>
  )
}
