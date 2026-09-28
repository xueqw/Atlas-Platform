import type{Agent,Connector,Conversation,KnowledgeBase,ModelCatalog,ModelTestResult,Source}from'./types'
const json=async<T>(url:string,options?:RequestInit):Promise<T>=>{const r=await fetch(url,{headers:{'Content-Type':'application/json'},...options});if(!r.ok)throw new Error((await r.json()).detail||'Request failed');return r.json()}
export const listConversations=()=>json<Conversation[]>('/api/conversations')
export const createConversation=()=>json<Conversation>('/api/conversations',{method:'POST',body:JSON.stringify({title:'New task'})})
export const getConversation=(id:string)=>json<Conversation>(`/api/conversations/${id}`)
export const listKnowledgeBases=()=>json<KnowledgeBase[]>('/api/knowledge-bases')
export const createKnowledgeBase=(name:string,description:string)=>json<KnowledgeBase>('/api/knowledge-bases',{method:'POST',body:JSON.stringify({name,description})})
export async function uploadDocument(id:string,file:File){const body=new FormData();body.append('file',file);const r=await fetch(`/api/knowledge-bases/${id}/documents`,{method:'POST',body});if(!r.ok)throw new Error((await r.json()).detail||'Upload failed');return r.json()}
export const deleteDocument=(id:string)=>fetch(`/api/documents/${id}`,{method:'DELETE'}).then(r=>{if(!r.ok)throw new Error('Delete failed')})
export const deleteConversation=(id:string)=>fetch(`/api/conversations/${id}`,{method:'DELETE'}).then(r=>{if(!r.ok)throw new Error('Delete failed')})
export const listModels=()=>json<ModelCatalog>('/api/models')
export const listConnectors=()=>json<{connectors:Connector[]}>('/api/connectors').then(r=>r.connectors)
export const configureGithub=(pat:string)=>json<Connector>('/api/connectors/github/config',{method:'POST',body:JSON.stringify({pat})})
export const disconnectGithub=()=>fetch('/api/connectors/github',{method:'DELETE'}).then(r=>{if(!r.ok)throw new Error('Disconnect failed')})
export const testModel=(model:string)=>json<ModelTestResult>('/api/models/test',{method:'POST',body:JSON.stringify({model})})
export const listAgents=()=>json<Agent[]>('/api/agents')
export const createAgent=(name:string)=>json<Agent>('/api/agents',{method:'POST',body:JSON.stringify({name,description:'',system_prompt:'You are a reliable enterprise AI assistant. Give clear answers, cite available sources, and state when information is missing.',model:'gpt-4.1-mini',knowledge_base_id:null})})
export const updateAgent=(agent:Agent)=>json<Agent>(`/api/agents/${agent.id}`,{method:'PUT',body:JSON.stringify(agent)})
export const deleteAgent=(id:string)=>fetch(`/api/agents/${id}`,{method:'DELETE'}).then(r=>{if(!r.ok)throw new Error('Delete failed')})
export async function uploadAttachment(file:File):Promise<{name:string;text:string}>{const body=new FormData();body.append('file',file);const r=await fetch('/api/attachments',{method:'POST',body});if(!r.ok)throw new Error((await r.json()).detail||'Could not parse the file');return r.json()}
export async function streamMessage(id:string,content:string,knowledgeBaseId:string|undefined,onToken:(token:string)=>void,onSources:(sources:Source[])=>void,agentId?:string,model?:string,attachment?:{name:string;text:string},connectors?:string[]){const r=await fetch(`/api/conversations/${id}/messages/stream`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,knowledge_base_id:knowledgeBaseId||null,agent_id:agentId||null,model:model||null,connectors:connectors||[],attachment_name:attachment?.name||null,attachment_text:attachment?.text||null})});if(!r.ok)throw new Error((await r.json()).detail||'Message failed');const reader=r.body?.getReader();if(!reader)return;const decoder=new TextDecoder();let buffer='';while(true){const{done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});const blocks=buffer.split('\n\n');buffer=blocks.pop()||'';for(const block of blocks){const line=block.split('\n').find(v=>v.startsWith('data: '));if(!line)continue;const data=JSON.parse(line.slice(6));if(data.type==='token')onToken(data.content);if(data.type==='sources')onSources(data.sources);if(data.type==='error')throw new Error(data.message)}}}
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
    body: JSON.stringify({ message, project_name: projectName })
  })

export const createAppDraft = (name: string) =>
  json<AppDraft>('/api/apps/drafts', {
    method: 'POST',
    body: JSON.stringify({ name })
  })

export const listDraftFiles = (draftId: string) =>
  json<AppFile[]>(`/api/apps/drafts/${draftId}/files`)

export const createDraftFile = (draftId: string, path: string, content = '') =>
  json<{ ok: boolean; path: string }>(`/api/apps/drafts/${draftId}/files`, {
    method: 'POST',
    body: JSON.stringify({ path, content })
  })

export const getDraftFile = (draftId: string, path: string) =>
  json<DraftFileContent>(
    `/api/apps/drafts/${draftId}/files/content?path=${encodeURIComponent(path)}`
  )

export const saveDraftFile = (draftId: string, path: string, content: string) =>
  json<{ ok: boolean }>(`/api/apps/drafts/${draftId}/files/content`, {
    method: 'PUT',
    body: JSON.stringify({ path, content })
  })

export const renameDraftFile = (draftId: string, oldPath: string, newPath: string) =>
  json<{ ok: boolean; path: string }>(`/api/apps/drafts/${draftId}/files/rename`, {
    method: 'PUT',
    body: JSON.stringify({ old_path: oldPath, new_path: newPath })
  })

export const deleteDraftFile = (draftId: string, path: string) =>
  fetch(`/api/apps/drafts/${draftId}/files?path=${encodeURIComponent(path)}`, {
    method: 'DELETE'
  }).then(r => {
    if (!r.ok) throw new Error('Delete failed')
    return r.json()
  })

export const validateDraftManifest = (draftId: string) =>
  json<ManifestValidation>(`/api/apps/drafts/${draftId}/manifest/validate`)

export const runDraftApp = (draftId: string, inputText: string) =>
  json<RunPreviewResult>(`/api/apps/drafts/${draftId}/run`, {
    method: 'POST',
    body: JSON.stringify({ input_text: inputText })
  })

export const evaluateDraftApp = (draftId: string, cases: EvaluationCase[]) =>
  json<EvaluationResult>(`/api/apps/drafts/${draftId}/evaluate`, {
    method: 'POST',
    body: JSON.stringify({ cases })
  })
