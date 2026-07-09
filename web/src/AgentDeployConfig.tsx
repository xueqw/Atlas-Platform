import { useEffect, useState } from 'react'
import {
  getDeployConfig,
  listAgentCallLogs,
  listConnectors,
  listKnowledgeBases,
  listSkills,
  listWorkspaceMembers,
  updateDeployConfig,
} from './api'
import type { CallLogEntry, Connector, DeployConfig, KnowledgeBase, Skill, WorkspaceMember } from './types'
import type { Agent } from './types'

const visibilityLabel: Record<string, string> = {
  private: '仅自己',
  shared: '指定用户',
  workspace: '工作空间成员',
  marketplace: '组织应用市场',
}

export default function AgentDeployConfig({
  agent,
  onClose,
  notice,
}: {
  agent: Agent
  onClose: () => void
  notice: (text: string) => void
}) {
  const [config, setConfig] = useState<DeployConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [knowledge, setKnowledge] = useState<KnowledgeBase[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [connectors, setConnectors] = useState<Connector[]>([])
  const [logs, setLogs] = useState<CallLogEntry[]>([])

  useEffect(() => {
    Promise.all([
      getDeployConfig(agent.id),
      listWorkspaceMembers(),
      listKnowledgeBases(),
      listSkills(),
      listConnectors(),
      listAgentCallLogs(agent.id),
    ])
      .then(([cfg, memberList, kbList, skillList, connectorList, logList]) => {
        setConfig(cfg)
        setMembers(memberList)
        setKnowledge(kbList)
        setSkills(skillList)
        setConnectors(connectorList)
        setLogs(logList)
      })
      .catch(() => notice('加载落地配置失败'))
      .finally(() => setLoading(false))
  }, [agent.id])

  function update<K extends keyof DeployConfig>(key: K, value: DeployConfig[K]) {
    setConfig(old => (old ? { ...old, [key]: value } : old))
  }

  function toggleInList(key: 'shared_user_ids' | 'allowed_knowledge_base_ids' | 'allowed_skill_ids' | 'allowed_connectors', id: string) {
    if (!config) return
    const list = config[key]
    update(key, list.includes(id) ? list.filter(v => v !== id) : [...list, id])
  }

  async function save() {
    if (!config) return
    setSaving(true)
    try {
      await updateDeployConfig(agent.id, config)
      notice('落地配置已保存')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="run-drawer-mask" onClick={onClose}>
      <aside className="run-drawer deploy-config-drawer" onClick={e => e.stopPropagation()}>
        <header>
          <strong>落地配置 · {agent.name}</strong>
          <button className="rd-close" onClick={onClose}>×</button>
        </header>

        {loading || !config ? (
          <p className="rd-empty">加载中…</p>
        ) : (
          <div className="deploy-config-body">
            <section className="deploy-config-section">
              <h4>可见范围</h4>
              <div className="deploy-visibility-options">
                {(['private', 'shared', 'workspace', 'marketplace'] as const).map(option => (
                  <label key={option} className="deploy-radio">
                    <input
                      type="radio"
                      name="visibility"
                      checked={config.visibility === option}
                      onChange={() => update('visibility', option)}
                    />
                    {visibilityLabel[option]}
                  </label>
                ))}
              </div>
              {config.visibility === 'shared' && (
                <div className="deploy-checklist">
                  {members.length === 0 && <p className="rd-empty">工作区暂无其他成员</p>}
                  {members.map(member => (
                    <label key={member.id} className="deploy-checkbox">
                      <input
                        type="checkbox"
                        checked={config.shared_user_ids.includes(member.id)}
                        onChange={() => toggleInList('shared_user_ids', member.id)}
                      />
                      {member.name}<small>@{member.username}</small>
                    </label>
                  ))}
                </div>
              )}
            </section>

            <section className="deploy-config-section">
              <h4>知识库权限<small>不勾选=不限制</small></h4>
              <div className="deploy-checklist">
                {knowledge.map(kb => (
                  <label key={kb.id} className="deploy-checkbox">
                    <input
                      type="checkbox"
                      checked={config.allowed_knowledge_base_ids.includes(kb.id)}
                      onChange={() => toggleInList('allowed_knowledge_base_ids', kb.id)}
                    />
                    {kb.name}
                  </label>
                ))}
                {knowledge.length === 0 && <p className="rd-empty">暂无知识库</p>}
              </div>
            </section>

            <section className="deploy-config-section">
              <h4>Skills 权限<small>不勾选=不限制</small></h4>
              <div className="deploy-checklist">
                {skills.map(skill => (
                  <label key={skill.id} className="deploy-checkbox">
                    <input
                      type="checkbox"
                      checked={config.allowed_skill_ids.includes(skill.id)}
                      onChange={() => toggleInList('allowed_skill_ids', skill.id)}
                    />
                    {skill.name}
                  </label>
                ))}
                {skills.length === 0 && <p className="rd-empty">暂无 Skills</p>}
              </div>
            </section>

            <section className="deploy-config-section">
              <h4>连接器权限<small>不勾选=不限制</small></h4>
              <div className="deploy-checklist">
                {connectors.map(connector => (
                  <label key={connector.provider} className="deploy-checkbox">
                    <input
                      type="checkbox"
                      checked={config.allowed_connectors.includes(connector.provider)}
                      onChange={() => toggleInList('allowed_connectors', connector.provider)}
                    />
                    {connector.name}
                  </label>
                ))}
                {connectors.length === 0 && <p className="rd-empty">暂无连接器</p>}
              </div>
            </section>

            <section className="deploy-config-section deploy-toggles">
              <label className="deploy-toggle-line">
                <input
                  type="checkbox"
                  checked={config.write_confirm}
                  onChange={e => update('write_confirm', e.target.checked)}
                />
                写操作前二次确认
              </label>
              <label className="deploy-toggle-line">
                <input
                  type="checkbox"
                  checked={config.call_log_enabled}
                  onChange={e => update('call_log_enabled', e.target.checked)}
                />
                记录调用日志
              </label>
              <label className="deploy-toggle-line">
                <input
                  type="checkbox"
                  checked={config.api_access}
                  onChange={e => update('api_access', e.target.checked)}
                />
                允许外部 API 调用
                <small>占位，需要 API Key 功能完成后生效</small>
              </label>
            </section>

            <div className="deploy-config-actions">
              <button className="solid" onClick={save} disabled={saving}>{saving ? '保存中…' : '保存配置'}</button>
            </div>

            <section className="deploy-config-section">
              <h4>调用日志<small>最近 200 条</small></h4>
              <table className="deploy-log-table">
                <thead>
                  <tr><th>时间</th><th>来源</th><th>状态</th><th>耗时</th><th>错误</th></tr>
                </thead>
                <tbody>
                  {logs.map(entry => (
                    <tr key={entry.id}>
                      <td>{new Date(entry.time).toLocaleString()}</td>
                      <td>{entry.source}</td>
                      <td>{entry.status}</td>
                      <td>{entry.latency_ms != null ? `${entry.latency_ms}ms` : '—'}</td>
                      <td>{entry.error || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {logs.length === 0 && <p className="rd-empty">{config.call_log_enabled ? '暂无调用记录' : '调用日志已关闭'}</p>}
            </section>
          </div>
        )}
      </aside>
    </div>
  )
}
