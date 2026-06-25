import type{Agent,Connector,Conversation,KnowledgeBase,ModelCatalog,ModelTestResult,Source}from'./types'
const json=async<T>(url:string,options?:RequestInit):Promise<T>=>{const r=await fetch(url,{headers:{'Content-Type':'application/json'},...options});if(!r.ok)throw new Error((await r.json()).detail||'请求失败');return r.json()}
export const listConversations=()=>json<Conversation[]>('/api/conversations')
export const createConversation=()=>json<Conversation>('/api/conversations',{method:'POST',body:JSON.stringify({title:'新任务'})})
export const getConversation=(id:string)=>json<Conversation>(`/api/conversations/${id}`)
export const listKnowledgeBases=()=>json<KnowledgeBase[]>('/api/knowledge-bases')
export const createKnowledgeBase=(name:string,description:string)=>json<KnowledgeBase>('/api/knowledge-bases',{method:'POST',body:JSON.stringify({name,description})})
export async function uploadDocument(id:string,file:File){const body=new FormData();body.append('file',file);const r=await fetch(`/api/knowledge-bases/${id}/documents`,{method:'POST',body});if(!r.ok)throw new Error((await r.json()).detail||'上传失败');return r.json()}
export const deleteDocument=(id:string)=>fetch(`/api/documents/${id}`,{method:'DELETE'}).then(r=>{if(!r.ok)throw new Error('删除失败')})
export const deleteConversation=(id:string)=>fetch(`/api/conversations/${id}`,{method:'DELETE'}).then(r=>{if(!r.ok)throw new Error('删除失败')})
export const listModels=()=>json<ModelCatalog>('/api/models')
export const listConnectors=()=>json<{connectors:Connector[]}>('/api/connectors').then(r=>r.connectors)
export const configureGithub=(pat:string)=>json<Connector>('/api/connectors/github/config',{method:'POST',body:JSON.stringify({pat})})
export const disconnectGithub=()=>fetch('/api/connectors/github',{method:'DELETE'}).then(r=>{if(!r.ok)throw new Error('断开失败')})
export const configureFeishu=(app_id:string,app_secret:string)=>json<Connector>('/api/connectors/feishu/config',{method:'POST',body:JSON.stringify({app_id,app_secret})})
export const disconnectFeishu=()=>fetch('/api/connectors/feishu/config',{method:'DELETE'}).then(r=>{if(!r.ok)throw new Error('断开失败')})
export const testModel=(model:string)=>json<ModelTestResult>('/api/models/test',{method:'POST',body:JSON.stringify({model})})
export const listAgents=()=>json<Agent[]>('/api/agents')
export const createAgent=(name:string)=>json<Agent>('/api/agents',{method:'POST',body:JSON.stringify({name,description:'',system_prompt:'你是一名可靠、严谨的企业智能助手。',model:'gpt-4.1-mini',knowledge_base_id:null})})
export const updateAgent=(agent:Agent)=>json<Agent>(`/api/agents/${agent.id}`,{method:'PUT',body:JSON.stringify(agent)})
export const deleteAgent=(id:string)=>fetch(`/api/agents/${id}`,{method:'DELETE'}).then(r=>{if(!r.ok)throw new Error('删除失败')})
export async function uploadAttachment(file:File):Promise<{name:string;text:string}>{const body=new FormData();body.append('file',file);const r=await fetch('/api/attachments',{method:'POST',body});if(!r.ok)throw new Error((await r.json()).detail||'文件解析失败');return r.json()}
export async function streamMessage(id:string,content:string,knowledgeBaseId:string|undefined,onToken:(token:string)=>void,onSources:(sources:Source[])=>void,agentId?:string,model?:string,attachment?:{name:string;text:string},connectors?:string[]){const r=await fetch(`/api/conversations/${id}/messages/stream`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,knowledge_base_id:knowledgeBaseId||null,agent_id:agentId||null,model:model||null,connectors:connectors||[],attachment_name:attachment?.name||null,attachment_text:attachment?.text||null})});if(!r.ok)throw new Error((await r.json()).detail||'发送失败');const reader=r.body?.getReader();if(!reader)return;const decoder=new TextDecoder();let buffer='';while(true){const{done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});const blocks=buffer.split('\n\n');buffer=blocks.pop()||'';for(const block of blocks){const line=block.split('\n').find(v=>v.startsWith('data: '));if(!line)continue;const data=JSON.parse(line.slice(6));if(data.type==='token')onToken(data.content);if(data.type==='sources')onSources(data.sources);if(data.type==='error')throw new Error(data.message)}}}
