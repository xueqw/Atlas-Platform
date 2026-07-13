import { useEffect, useState } from 'react'
import { archiveAgent, copyAgent, createAgent, createAppDraft, deleteAgent, listAgents, unarchiveAgent } from './api'
import type { Agent } from './types'
import AgentApiKeyDialog from './AgentApiKeyDialog'
import AgentDeployConfig from './AgentDeployConfig'
import AgentEvaluationPanel from './AgentEvaluationPanel'
import AgentFlowSteps from './AgentFlowSteps'
import AgentTemplateDialog from './AgentTemplateDialog'
import AgentVersionsDrawer from './AgentVersions'
import PublishChecklistDialog from './PublishChecklistDialog'

const statusLabel: Record<string, string> = {
  draft: '草稿中',
  published: '已发布',
  archived: '已下架',
}

const stageLabel: Record<string, string> = {
  develop: '开发中',
  test: '待测试',
  evaluate: '待评测',
  deploy: '待配置',
  publish: '待发布',
  published: '已发布',
  archived: '已下架',
  risk: '有风险',
}

const healthLabel: Record<string, string> = {
  healthy: '健康',
  attention: '需关注',
  risk: '有风险',
  unknown: '待验证',
}

function timeAgo(iso: string) {
  const diffMs = Date.now() - new Date(iso).getTime()
  const minutes = Math.floor(diffMs / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  return `${days} 天前`
}

export default function AgentsList({
  onOpenCode,
  onOpenPrompt,
  onInvoke,
  onNewProject,
  notice,
}: {
  onOpenCode: (agent: Agent) => void
  onOpenPrompt: (agent: Agent) => void
  onInvoke: (agent: Agent) => void
  onNewProject: () => void
  notice: (text: string) => void
}) {
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(true)
  const [versionsFor, setVersionsFor] = useState<Agent | null>(null)
  const [deployConfigFor, setDeployConfigFor] = useState<Agent | null>(null)
  const [evaluationFor, setEvaluationFor] = useState<Agent | null>(null)
  const [publishChecklistFor, setPublishChecklistFor] = useState<Agent | null>(null)
  const [apiKeyFor, setApiKeyFor] = useState<Agent | null>(null)
  const [templateDialogOpen, setTemplateDialogOpen] = useState(false)
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [stageFilter, setStageFilter] = useState('all')
  const [kindFilter, setKindFilter] = useState('all')

  const refresh = () => listAgents().then(setAgents).finally(() => setLoading(false))

  useEffect(() => {
    void refresh()
  }, [])

  async function newPromptAgent() {
    const item = await createAgent(`新智能体 ${agents.length + 1}`)
    notice('智能体已创建')
    await refresh()
    setCreateDialogOpen(false)
    onOpenPrompt(item)
  }

  async function newCodeAgent() {
    const item = await createAppDraft(`新应用 ${agents.length + 1}`)
    notice('应用草稿已创建')
    await refresh()
    setCreateDialogOpen(false)
    onOpenCode({ id: item.id, name: item.name, status: item.status } as Agent)
  }

  async function remove(agent: Agent) {
    if (!confirm(`删除智能体"${agent.name}"？`)) return
    await deleteAgent(agent.id)
    notice('智能体已删除')
    await refresh()
  }

  async function archive(agent: Agent) {
    if (!confirm(`下架智能体"${agent.name}"？下架后无法被调用，可随时重新上架。`)) return
    await archiveAgent(agent.id)
    notice('已下架')
    await refresh()
  }

  async function unarchive(agent: Agent) {
    await unarchiveAgent(agent.id)
    notice('已重新上架')
    await refresh()
  }

  async function copy(agent: Agent) {
    try {
      await copyAgent(agent.id)
      notice(`已复制"${agent.name}"`)
      await refresh()
    } catch (error) {
      notice(error instanceof Error ? error.message : '复制失败')
    }
  }

  const visibleAgents = agents.filter(agent => {
    const text = `${agent.name} ${agent.description}`.toLowerCase()
    return (!query.trim() || text.includes(query.trim().toLowerCase()))
      && (stageFilter === 'all' || agent.workflow_stage === stageFilter || agent.status === stageFilter)
      && (kindFilter === 'all' || agent.kind === kindFilter)
  })

  return (
    <section className="agents-list-page">
      <div className="agents-list-title">
        <div>
          <span className="section-code">AGENTS / MY AGENTS</span>
          <h1>我的 Agents</h1>
          <p>查看已创建的智能体，继续开发、查看版本或发布上线。</p>
        </div>
        <div className="agents-list-actions">
          <button className="solid" onClick={() => setCreateDialogOpen(true)}>+ 新建 Agent</button>
        </div>
      </div>

      <div className="agents-list-toolbar">
        <label><span>搜索</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="名称或描述" /></label>
        <label><span>阶段</span><select value={stageFilter} onChange={event => setStageFilter(event.target.value)}><option value="all">全部阶段</option><option value="develop">开发中</option><option value="test">待测试</option><option value="evaluate">待评测</option><option value="deploy">待配置</option><option value="publish">待发布</option><option value="published">已发布</option><option value="risk">有风险</option><option value="archived">已下架</option></select></label>
        <label><span>类型</span><select value={kindFilter} onChange={event => setKindFilter(event.target.value)}><option value="all">全部类型</option><option value="code">代码型</option><option value="prompt">Prompt 型</option></select></label>
        <b>{visibleAgents.length} / {agents.length}</b>
      </div>

      {loading ? (
        <p className="models-empty">正在加载 Agents…</p>
      ) : agents.length === 0 ? (
        <div className="agents-empty-state">
          <b>A</b>
          <h2>还没有 Agent</h2>
          <p>选择一种方式创建你的第一个智能体。</p>
          <button className="solid" onClick={newPromptAgent}>+ 创建智能体</button>
        </div>
      ) : (
        <div className="connector-grid agents-grid">
          {visibleAgents.map(agent => (
            <div className="connector-card agent-card" key={agent.id}>
              <div className="connector-card-head">
                <div className="connector-mark">{agent.name.slice(0, 1)}</div>
                <div>
                  <strong>
                    {agent.name}
                    <span className="conn-kind">{agent.kind === 'code' ? '代码型' : 'Prompt 型'}</span>
                  </strong>
                  <small>{agent.description || '暂无描述'}</small>
                </div>
                <span className={`provider-status ${agent.workflow_stage === 'published' ? 'on' : agent.workflow_stage === 'archived' ? 'archived' : agent.workflow_stage === 'risk' ? 'risk' : 'off'}`}>
                  {agent.workflow_stage === 'published' ? '● ' : agent.workflow_stage === 'archived' ? '◌ ' : '○ '}
                  {stageLabel[agent.workflow_stage] || statusLabel[agent.status] || agent.status}
                </span>
              </div>

              <div className="agent-card-meta">
                <span>v{agent.version_no}{agent.published_version_no ? ` · 已发布 v${agent.published_version_no}` : ''}</span>
                <span>{agent.knowledge_base_count} 知识库</span>
                <span>{agent.skills_count} Skills</span>
                <span>{agent.connector_count} 连接器</span>
                <span>调用 {agent.call_count} 次</span>
                <span>{timeAgo(agent.updated_at)}</span>
              </div>

              <div className={`agent-health-strip ${agent.health_status}`}>
                <span><i />{healthLabel[agent.health_status] || '待验证'}</span>
                <span>{agent.success_rate == null ? '暂无评测数据' : `最近通过率 ${Math.round(agent.success_rate * 100)}%`}</span>
              </div>

              <AgentFlowSteps
                agent={agent}
                onOpenCode={onOpenCode}
                onOpenPrompt={onOpenPrompt}
                onDeployConfig={setDeployConfigFor}
                onPublishChecklist={setPublishChecklistFor}
              />

              <div className="connector-card-foot">
                <span className="conn-account">{agent.model}</span>
                <div>
                  <button onClick={() => setVersionsFor(agent)}>版本</button>
                  <button onClick={() => setEvaluationFor(agent)}>效果评测</button>
                  <button onClick={() => setDeployConfigFor(agent)}>落地配置</button>
                  <button onClick={() => setApiKeyFor(agent)} disabled={agent.status !== 'published'} title={agent.status === 'published' ? '管理 API Key' : '发布后可管理 API Key'}>API Key</button>
                  <button onClick={() => (agent.kind === 'code' ? onOpenCode(agent) : onOpenPrompt(agent))}>继续开发</button>
                  <button className="solid" onClick={() => onInvoke(agent)}>运行</button>
                  {(agent.status === 'draft' || agent.workflow_stage === 'publish') && (
                    <button className="solid" onClick={() => setPublishChecklistFor(agent)}>发布</button>
                  )}
                  {agent.status === 'published' && (
                    <>
                      <button onClick={() => archive(agent)}>下架</button>
                    </>
                  )}
                  {agent.status === 'archived' && (
                    <button className="solid" onClick={() => unarchive(agent)}>重新上架</button>
                  )}
                  <button onClick={() => copy(agent)}>复制</button>
                  <button className="danger-link" onClick={() => remove(agent)}>删除</button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {versionsFor && (
        <AgentVersionsDrawer
          agent={versionsFor}
          onClose={() => setVersionsFor(null)}
          onChanged={refresh}
          notice={notice}
        />
      )}

      {deployConfigFor && (
        <AgentDeployConfig
          agent={deployConfigFor}
          onClose={() => setDeployConfigFor(null)}
          notice={notice}
        />
      )}

      {evaluationFor && (
        <AgentEvaluationPanel
          agent={evaluationFor}
          onClose={() => setEvaluationFor(null)}
          notice={notice}
        />
      )}

      {publishChecklistFor && (
        <PublishChecklistDialog
          agent={publishChecklistFor}
          onClose={() => setPublishChecklistFor(null)}
          onPublished={refresh}
          notice={notice}
        />
      )}

      {apiKeyFor && (
        <AgentApiKeyDialog
          agent={apiKeyFor}
          onClose={() => setApiKeyFor(null)}
          notice={notice}
        />
      )}

      {templateDialogOpen && (
        <AgentTemplateDialog
          onClose={() => setTemplateDialogOpen(false)}
          onCreated={async agent => {
            await refresh()
            onOpenPrompt(agent)
          }}
          notice={notice}
        />
      )}

      {createDialogOpen && (
        <div className="dialog-backdrop" onMouseDown={() => setCreateDialogOpen(false)}>
          <div className="dialog new-agent-dialog" onMouseDown={event => event.stopPropagation()}>
            <div className="new-agent-dialog-head"><span>CREATE AGENT</span><h2>选择创建方式</h2><p>推荐使用会话式开发，平台会创建项目、测试和发布流程。</p></div>
            <div className="new-agent-options">
              <button className="recommended" type="button" onClick={() => { setCreateDialogOpen(false); onNewProject() }}><b>会话式开发</b><span>描述需求，持续修改同一个代码型 Agent</span><i>推荐</i></button>
              <button type="button" onClick={newCodeAgent}><b>空白代码项目</b><span>从标准 Atlas Agent 框架开始</span></button>
              <button type="button" onClick={() => { setCreateDialogOpen(false); setTemplateDialogOpen(true) }}><b>从模板创建</b><span>使用预置业务模板快速开始</span></button>
              <button type="button" onClick={newPromptAgent}><b>Prompt Agent</b><span>适合无需项目代码的轻量助手</span></button>
            </div>
            <div><button type="button" onClick={() => setCreateDialogOpen(false)}>取消</button></div>
          </div>
        </div>
      )}
    </section>
  )
}
