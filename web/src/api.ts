import type {
  Account,
  Agent,
  AgentTemplate,
  AgentVersion,
  AgentVersionDetail,
  AgentVersionDiff,
  ApiKey,
  ApiKeyCreated,
  CallLogEntry,
  Connector,
  Conversation,
  DeployConfig,
  EvaluationRunV2,
  EvaluationRunComparison,
  EvaluationRunHistory,
  EvaluationSuite,
  KnowledgeBase,
  Me,
  ModelCatalog,
  ModelTestResult,
  Plan,
  PublishChecklist,
  Skill,
  Source,
  WorkflowRun,
  RuntimeRunHandle,
  RuntimeRunState,
  WorkspaceMember,
  GovernedMemory,
  SkillDiscovery,
  OrchestrationPlan,
  OrchestrationExecution,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')
const apiUrl = (path: string) => `${API_BASE}${path}`
export const runtimeEventStreamUrl = (runId: string, cursor: number) => apiUrl(`/api/runtime/runs/${encodeURIComponent(runId)}/events/stream?after_sequence=${cursor}`)

async function errorDetail(response: Response, fallback: string) {
  try {
    return (await response.json()).detail || fallback
  } catch {
    return fallback
  }
}

const json = async <T>(url: string, options?: RequestInit): Promise<T> => {
  const response = await fetch(apiUrl(url), {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!response.ok) throw new Error(await errorDetail(response, '请求失败'))
  return response.json()
}

export const getMe = () => json<Me>('/api/me')
export const listAccounts = () => json<Account[]>('/api/auth/accounts')
export const login = (username: string, password: string) =>
  json<Me>('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) })
export const logout = () => fetch(apiUrl('/api/auth/logout'), { method: 'POST', credentials: 'include' }).then(() => {})
export const listConversations = () => json<Conversation[]>('/api/conversations')
export const createConversation = () =>
  json<Conversation>('/api/conversations', { method: 'POST', body: JSON.stringify({ title: '新任务' }) })
export const getConversation = (id: string) => json<Conversation>(`/api/conversations/${id}`)
export const listRuns = (id: string) => json<WorkflowRun[]>(`/api/conversations/${id}/runs`)
export const startRuntimeRun = (payload: { agent_id: string; input: string; idempotency_key: string; conversation_id?: string; source?: 'workbench' | 'builder' | 'subagent' | 'chat' | 'preview' | 'api' | 'evaluation' }) =>
  json<RuntimeRunHandle>('/api/runtime/runs', { method: 'POST', body: JSON.stringify({ ...payload, source: payload.source || 'workbench' }) })
export const getRuntimeRun = (runId: string) => json<RuntimeRunState>(`/api/runtime/runs/${runId}`)
export const discoverSkills = () => json<SkillDiscovery>('/api/skills/discovery')
export const listGovernedMemories = (agentId: string) => json<GovernedMemory>(`/api/agents/${agentId}/memory/facts`)
export const createGovernedMemory = (agentId: string, payload: { subject: string; predicate: string; object_value: string; evidence?: string }) =>
  json<{ id: string; created: boolean }>(`/api/agents/${agentId}/memory/facts`, { method: 'POST', body: JSON.stringify({ ...payload, idempotency_key: crypto.randomUUID() }) })
export const deleteGovernedMemory = (agentId: string, factId: string) =>
  json<{ id: string; tombstoned: boolean }>(`/api/agents/${agentId}/memory/facts/${factId}`, { method: 'DELETE', body: JSON.stringify({ idempotency_key: crypto.randomUUID(), reason: 'manual_workspace_delete' }) })
export const planOrchestration = (agentId: string, goal: string) =>
  json<OrchestrationPlan>('/api/orchestrations/plan', { method: 'POST', body: JSON.stringify({ agent_id: agentId, goal, max_workers: 4, concurrency: 3, max_replans: 2 }) })
export const executeOrchestration = (runId: string) =>
  json<OrchestrationExecution>(`/api/orchestrations/${runId}/execute`, { method: 'POST' })
export const listKnowledgeBases = () => json<KnowledgeBase[]>('/api/knowledge-bases')
export const createKnowledgeBase = (name: string, description: string) =>
  json<KnowledgeBase>('/api/knowledge-bases', { method: 'POST', body: JSON.stringify({ name, description }) })
export async function uploadDocument(id: string, file: File) {
  const body = new FormData()
  body.append('file', file)
  const response = await fetch(apiUrl(`/api/knowledge-bases/${id}/documents`), {
    method: 'POST',
    credentials: 'include',
    body,
  })
  if (!response.ok) throw new Error(await errorDetail(response, '上传失败'))
  return response.json()
}
export const deleteDocument = (id: string) =>
  fetch(apiUrl(`/api/documents/${id}`), { method: 'DELETE', credentials: 'include' }).then((response) => {
    if (!response.ok) throw new Error('删除失败')
  })
export const deleteConversation = (id: string) =>
  fetch(apiUrl(`/api/conversations/${id}`), { method: 'DELETE', credentials: 'include' }).then((response) => {
    if (!response.ok) throw new Error('删除失败')
  })
export const listModels = () => json<ModelCatalog>('/api/models')
export const listConnectors = () => json<{ connectors: Connector[] }>('/api/connectors').then((r) => r.connectors)
export const configureGithub = (pat: string) =>
  json<Connector>('/api/connectors/github/config', { method: 'POST', body: JSON.stringify({ pat }) })
export const disconnectGithub = () =>
  fetch(apiUrl('/api/connectors/github'), { method: 'DELETE', credentials: 'include' }).then((response) => {
    if (!response.ok) throw new Error('断开失败')
  })
export const configureFeishu = (app_id: string, app_secret: string) =>
  json<Connector>('/api/connectors/feishu/config', { method: 'POST', body: JSON.stringify({ app_id, app_secret }) })
export const disconnectFeishu = () =>
  fetch(apiUrl('/api/connectors/feishu/config'), { method: 'DELETE', credentials: 'include' }).then((response) => {
    if (!response.ok) throw new Error('断开失败')
  })
export const configureMcp = (provider: string, key: string) =>
  json<Connector>(`/api/connectors/mcp/${provider}/config`, { method: 'POST', body: JSON.stringify({ key }) })
export const disconnectMcp = (provider: string) =>
  fetch(apiUrl(`/api/connectors/mcp/${provider}`), { method: 'DELETE', credentials: 'include' }).then((response) => {
    if (!response.ok) throw new Error('断开失败')
  })
export const testModel = (model: string) =>
  json<ModelTestResult>('/api/models/test', { method: 'POST', body: JSON.stringify({ model }) })
export const configureModelProvider = (provider_id: string, api_key: string, base_url?: string) =>
  json<ModelCatalog>('/api/models/config', {
    method: 'POST',
    body: JSON.stringify({ provider_id, api_key, base_url: base_url || null }),
  })
export const listSkills = () => json<Skill[]>('/api/skills')
export const createSkill = (s: { name: string; description: string; content: string; trigger_phrases: string }) =>
  json<Skill>('/api/skills', { method: 'POST', body: JSON.stringify(s) })
export const updateSkill = (
  id: string,
  s: { name: string; description: string; content: string; trigger_phrases: string; status: string },
) => json<Skill>(`/api/skills/${id}`, { method: 'PUT', body: JSON.stringify(s) })
export const deleteSkill = (id: string) =>
  fetch(apiUrl(`/api/skills/${id}`), { method: 'DELETE', credentials: 'include' }).then((response) => {
    if (!response.ok) throw new Error('删除失败')
  })
export const listAgents = () => json<Agent[]>('/api/agents')
export const createAgent = (name: string) =>
  json<Agent>('/api/agents', {
    method: 'POST',
    body: JSON.stringify({
      name,
      description: '',
      system_prompt: '你是一名可靠、严谨的企业智能助手。',
      model: 'gpt-4.1-mini',
      knowledge_base_id: null,
    }),
  })
export const updateAgent = (agent: Agent) =>
  json<Agent>(`/api/agents/${agent.id}`, { method: 'PUT', body: JSON.stringify(agent) })
export const deleteAgent = (id: string) =>
  fetch(apiUrl(`/api/agents/${id}`), { method: 'DELETE' }).then((response) => {
    if (!response.ok) throw new Error('删除失败')
  })
export async function uploadAttachment(file: File): Promise<{ name: string; text: string }> {
  const body = new FormData()
  body.append('file', file)
  const response = await fetch(apiUrl('/api/attachments'), { method: 'POST', credentials: 'include', body })
  if (!response.ok) throw new Error(await errorDetail(response, '文件解析失败'))
  return response.json()
}
export async function streamMessage(
  id: string,
  content: string,
  knowledgeBaseId: string | undefined,
  onToken: (token: string) => void,
  onSources: (sources: Source[]) => void,
  agentId?: string,
  model?: string,
  attachment?: { name: string; text: string },
  connectors?: string[],
  onStep?: (label: string | null) => void,
  onPlan?: (plan: Plan) => void,
  skillIds?: string[],
  onSkills?: (skills: { id: string; name: string; source: string }[]) => void,
  onTool?: (t: { phase: 'call' | 'result'; name: string; access?: string; content?: string }) => void,
) {
  const response = await fetch(apiUrl(`/api/conversations/${id}/messages/stream`), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      content,
      knowledge_base_id: knowledgeBaseId || null,
      agent_id: agentId || null,
      model: model || null,
      connectors: connectors || [],
      skill_ids: skillIds || [],
      attachment_name: attachment?.name || null,
      attachment_text: attachment?.text || null,
    }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, '发送失败'))
  const reader = response.body?.getReader()
  if (!reader) return
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() || ''
    for (const block of blocks) {
      const line = block.split('\n').find((v) => v.startsWith('data: '))
      if (!line) continue
      const data = JSON.parse(line.slice(6))
      if (data.type === 'token') onToken(data.content)
      if (data.type === 'sources') onSources(data.sources)
      if (data.type === 'planning_started') onStep?.('规划任务')
      if (data.type === 'plan_created') onPlan?.(data.plan)
      if (data.type === 'skills_selected') onSkills?.(data.skills)
      if (data.type === 'tool_call') onTool?.({ phase: 'call', name: data.name, access: data.access })
      if (data.type === 'tool_result') onTool?.({ phase: 'result', name: data.name, content: data.content })
      if (data.type === 'step_started') onStep?.(data.title)
      if (data.type === 'run_completed' || data.type === 'run_failed') onStep?.(null)
      if (data.type === 'error') throw new Error(data.message)
    }
  }
}

export type AppDraft = {
  id: string
  name: string
  status: string
  created_at?: string
  updated_at?: string
}

export type AppFile = {
  path: string
  name: string
  type: 'file' | 'directory'
}

export type DraftFileContent = {
  path: string
  content: string
}

export type ManifestValidation = {
  ok: boolean
  manifest: Record<string, unknown>
  errors: string[]
  warnings: string[]
}

export type RunPreviewResult = {
  ok: boolean
  answer: string
  content?: string
  logs: string
  error?: string
  warnings?: string[]
  elapsed_ms?: number
  sources: Array<{ document: string; page: number; quote: string; score: number; knowledge_base_id?: string }>
  trace: Array<{ type: string; title: string; status: string; executor: string; output?: Record<string, unknown>; error?: string }>
  tool_calls: Array<{ name: string; status: string; provider: string }>
  requires_confirmation?: { tool: string; arguments: string; provider: string } | null
  version_no?: number | null
  version_label?: string
  run_id?: string | null
}

export type EvaluationCase = {
  name: string
  input: string
  expected: string
}

export type EvaluationSummary = {
  recommendation: 'publish' | 'optimize' | 'hold'
  recommendation_label: string
  avg_elapsed_ms: number
  declared_skills: string[]
  declared_connectors: string[]
  metrics?: Record<string, number | null>
  metric_notes?: Record<string, string>
}

export type EvaluationResult = {
  ok: boolean
  passed: number
  total: number
  pass_rate: number
  summary: EvaluationSummary
  results: Array<{
    name: string
    input: string
    expected: string
    ok: boolean
    output: string
    logs: string
    elapsed_ms: number
    failure_reason: string
    suggestion: string
    sources?: RunPreviewResult['sources']
    trace?: RunPreviewResult['trace']
    tool_calls?: RunPreviewResult['tool_calls']
  }>
}

export type BuilderMessage = {
  id: string
  role: 'assistant' | 'user'
  content: string
  created_at: string
  changed_files?: string[]
  version_no?: number
  warnings?: string[]
}

export type DraftProductState = {
  agent: Agent
  draft_dirty: boolean
  latest_test: null | {
    id: string
    kind: string
    ok: boolean
    passed: number
    total: number
    pass_rate: number
    avg_elapsed_ms: number
    results: RunPreviewResult[]
    created_at: string
  }
  latest_evaluation: null | {
    id: string
    kind: string
    ok: boolean
    passed: number
    total: number
    pass_rate: number
    avg_elapsed_ms: number
    results: EvaluationResult['results']
    summary: EvaluationSummary
    created_at: string
  }
  deploy_config: DeployConfig
  publish_checklist: PublishChecklist
  builder_messages: BuilderMessage[]
  preview_available: boolean
}

export type RefineAgentResult = {
  reply: string
  changed_files: string[]
  version_no: number
  warnings: string[]
  draft: AppDraft
}

export type DraftPreview = { exists: boolean; path: string; html: string }

export type GeneratedAgentApp = {
  draft: AppDraft
  reply: string
  blueprint: {
    name: string
    domain: string
    prompt: string
    skills: string[]
    connectors: string[]
    sample_input: string
    needs_preview?: boolean
  }
  files: string[]
}

export const generateAgentApp = (message: string, projectName = '') =>
  json<GeneratedAgentApp>('/api/apps/generate', {
    method: 'POST',
    body: JSON.stringify({ message, project_name: projectName }),
  })

export const refineAgentApp = (draftId: string, message: string) =>
  json<RefineAgentResult>(`/api/apps/drafts/${draftId}/refine`, {
    method: 'POST',
    body: JSON.stringify({ message }),
  })

export const getDraftProductState = (draftId: string) =>
  json<DraftProductState>(`/api/apps/drafts/${draftId}/product-state`)

export const getDraftPreview = (draftId: string) =>
  json<DraftPreview>(`/api/apps/drafts/${draftId}/preview`)

export const createAppDraft = (name: string) =>
  json<AppDraft>('/api/apps/drafts', {
    method: 'POST',
    body: JSON.stringify({ name }),
  })

export const listDraftFiles = (draftId: string) => json<AppFile[]>(`/api/apps/drafts/${draftId}/files`)

export const createDraftFile = (draftId: string, path: string, content = '') =>
  json<{ ok: boolean; path: string }>(`/api/apps/drafts/${draftId}/files`, {
    method: 'POST',
    body: JSON.stringify({ path, content }),
  })

export const getDraftFile = (draftId: string, path: string) =>
  json<DraftFileContent>(`/api/apps/drafts/${draftId}/files/content?path=${encodeURIComponent(path)}`)

export const saveDraftFile = (draftId: string, path: string, content: string) =>
  json<{ ok: boolean }>(`/api/apps/drafts/${draftId}/files/content`, {
    method: 'PUT',
    body: JSON.stringify({ path, content }),
  })

export const renameDraftFile = (draftId: string, oldPath: string, newPath: string) =>
  json<{ ok: boolean; path: string }>(`/api/apps/drafts/${draftId}/files/rename`, {
    method: 'PUT',
    body: JSON.stringify({ old_path: oldPath, new_path: newPath }),
  })

export const deleteDraftFile = (draftId: string, path: string) =>
  fetch(apiUrl(`/api/apps/drafts/${draftId}/files?path=${encodeURIComponent(path)}`), {
    method: 'DELETE',
    credentials: 'include',
  }).then((response) => {
    if (!response.ok) throw new Error('删除失败')
    return response.json()
  })

export const validateDraftManifest = (draftId: string) =>
  json<ManifestValidation>(`/api/apps/drafts/${draftId}/manifest/validate`)

export const runDraftApp = (draftId: string, inputText: string, confirmedTools: string[] = []) =>
  json<RunPreviewResult>(`/api/apps/drafts/${draftId}/run`, {
    method: 'POST',
    body: JSON.stringify({ input_text: inputText, confirmed_tools: confirmedTools }),
  })

export const evaluateDraftApp = (draftId: string, cases: EvaluationCase[]) =>
  json<EvaluationResult>(`/api/apps/drafts/${draftId}/evaluate`, {
    method: 'POST',
    body: JSON.stringify({ cases }),
  })

export const listEvaluationSuites = (agentId: string) => json<EvaluationSuite[]>(`/api/agents/${agentId}/evaluation-suites`)
export const createEvaluationSuite = (agentId: string, payload: {name:string;description?:string;pass_threshold?:number;is_release_gate?:boolean}) => json<EvaluationSuite>(`/api/agents/${agentId}/evaluation-suites`, {method:'POST',body:JSON.stringify(payload)})
export const createEvaluationCase = (agentId: string, suiteId: string, payload: {name:string;input_text:string;expected_text?:string;scorers?:Record<string,unknown>;is_key?:boolean}) => json(`/api/agents/${agentId}/evaluation-suites/${suiteId}/cases`, {method:'POST',body:JSON.stringify(payload)})
export const deleteEvaluationCase = (agentId: string, suiteId: string, caseId: string) => json<{ok:boolean}>(`/api/agents/${agentId}/evaluation-suites/${suiteId}/cases/${caseId}`, {method:'DELETE'})
export const runEvaluationSuite = (agentId: string, suiteId: string) => json<EvaluationRunV2>(`/api/agents/${agentId}/evaluation-suites/${suiteId}/run`, {method:'POST'})
export const listEvaluationRuns = (agentId: string) => json<EvaluationRunHistory[]>(`/api/agents/${agentId}/evaluation-runs`)
export const compareEvaluationRuns = (agentId: string, baselineRunId: string, candidateRunId: string) =>
  json<EvaluationRunComparison>(`/api/agents/${agentId}/evaluation-runs/compare`, {method:'POST', body: JSON.stringify({baseline_run_id: baselineRunId, candidate_run_id: candidateRunId})})

export const listAgentVersions = (agentId: string) =>
  json<AgentVersion[]>(`/api/agents/${agentId}/versions`)

export const getAgentVersion = (agentId: string, versionId: string) =>
  json<AgentVersionDetail>(`/api/agents/${agentId}/versions/${versionId}`)

export const diffAgentVersions = (agentId: string, from: number, to: number) =>
  json<AgentVersionDiff>(`/api/agents/${agentId}/versions/diff?from=${from}&to=${to}`)

export const saveAgentVersion = (agentId: string, note = '') =>
  json<AgentVersion>(`/api/agents/${agentId}/versions`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  })

export const rollbackAgentVersion = (agentId: string, versionId: string) =>
  json<AgentVersion>(`/api/agents/${agentId}/versions/${versionId}/rollback`, { method: 'POST' })

export const publishAgent = (agentId: string) =>
  json<AgentVersion>(`/api/agents/${agentId}/publish`, { method: 'POST' })

export const archiveAgent = (agentId: string) =>
  json<Agent>(`/api/agents/${agentId}/archive`, { method: 'POST' })

export const unarchiveAgent = (agentId: string) =>
  json<Agent>(`/api/agents/${agentId}/unarchive`, { method: 'POST' })

export const getDeployConfig = (agentId: string) =>
  json<DeployConfig>(`/api/agents/${agentId}/deploy-config`)

export const updateDeployConfig = (agentId: string, config: DeployConfig) =>
  json<DeployConfig>(`/api/agents/${agentId}/deploy-config`, {
    method: 'PUT',
    body: JSON.stringify(config),
  })

export const listAgentCallLogs = (agentId: string) =>
  json<CallLogEntry[]>(`/api/agents/${agentId}/call-logs`)

export const listWorkspaceMembers = () =>
  json<WorkspaceMember[]>('/api/workspace/members')

export const getPublishChecklist = (agentId: string) =>
  json<PublishChecklist>(`/api/agents/${agentId}/publish-checklist`)

export const getApiKey = (agentId: string) =>
  json<ApiKey>(`/api/agents/${agentId}/api-key`)

export const createOrResetApiKey = (agentId: string) =>
  json<ApiKeyCreated>(`/api/agents/${agentId}/api-key`, { method: 'POST' })

export const updateApiKey = (
  agentId: string,
  patch: { status: string; expires_at: string | null; daily_quota: number | null; allowed_origins: string },
) =>
  json<ApiKey>(`/api/agents/${agentId}/api-key`, {
    method: 'PUT',
    body: JSON.stringify(patch),
  })

export const deleteApiKey = (agentId: string) =>
  fetch(apiUrl(`/api/agents/${agentId}/api-key`), { method: 'DELETE', credentials: 'include' }).then((response) => {
    if (!response.ok) throw new Error('删除失败')
  })

export const listAgentTemplates = () => json<AgentTemplate[]>('/api/agents/templates')

export const createAgentFromTemplate = (templateId: string) =>
  json<Agent>('/api/agents/from-template', {
    method: 'POST',
    body: JSON.stringify({ template_id: templateId }),
  })

export const copyAgent = (agentId: string) =>
  json<Agent>(`/api/agents/${agentId}/copy`, { method: 'POST' })
