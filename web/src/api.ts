import type {
  Account,
  Agent,
  Connector,
  Conversation,
  KnowledgeBase,
  Me,
  ModelCatalog,
  ModelTestResult,
  Plan,
  Skill,
  Source,
  WorkflowRun,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')
const apiUrl = (path: string) => `${API_BASE}${path}`

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
  logs: string
  error?: string
  warnings?: string[]
  elapsed_ms?: number
}

export type EvaluationCase = {
  name: string
  input: string
  expected: string
}

export type EvaluationResult = {
  ok: boolean
  passed: number
  total: number
  pass_rate: number
  results: Array<{
    name: string
    input: string
    expected: string
    ok: boolean
    output: string
    logs: string
    elapsed_ms: number
  }>
}

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
  }
  files: string[]
}

export const generateAgentApp = (message: string, projectName = '') =>
  json<GeneratedAgentApp>('/api/apps/generate', {
    method: 'POST',
    body: JSON.stringify({ message, project_name: projectName }),
  })

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

export const runDraftApp = (draftId: string, inputText: string) =>
  json<RunPreviewResult>(`/api/apps/drafts/${draftId}/run`, {
    method: 'POST',
    body: JSON.stringify({ input_text: inputText }),
  })

export const evaluateDraftApp = (draftId: string, cases: EvaluationCase[]) =>
  json<EvaluationResult>(`/api/apps/drafts/${draftId}/evaluate`, {
    method: 'POST',
    body: JSON.stringify({ cases }),
  })
