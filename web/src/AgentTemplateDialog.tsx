import { useEffect, useState } from 'react'
import { createAgentFromTemplate, listAgentTemplates } from './api'
import type { Agent, AgentTemplate } from './types'

export default function AgentTemplateDialog({
  onClose,
  onCreated,
  notice,
}: {
  onClose: () => void
  onCreated: (agent: Agent) => void
  notice: (text: string) => void
}) {
  const [templates, setTemplates] = useState<AgentTemplate[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    listAgentTemplates()
      .then(items => {
        setTemplates(items)
        setSelectedId(items[0]?.id || '')
      })
      .catch(() => notice('加载模板列表失败'))
      .finally(() => setLoading(false))
  }, [])

  async function create() {
    if (!selectedId) return
    setCreating(true)
    try {
      const agent = await createAgentFromTemplate(selectedId)
      notice('已从模板创建智能体')
      onCreated(agent)
      onClose()
    } catch (error) {
      notice(error instanceof Error ? error.message : '创建失败')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <div className="dialog agent-template-dialog" onMouseDown={e => e.stopPropagation()}>
        <h2>从模板创建</h2>

        {loading ? (
          <p className="rd-empty">加载中…</p>
        ) : (
          <div className="deploy-checklist agent-template-list">
            {templates.map(template => (
              <label className="deploy-radio agent-template-option" key={template.id}>
                <input
                  type="radio"
                  name="agent-template"
                  checked={selectedId === template.id}
                  onChange={() => setSelectedId(template.id)}
                />
                <div>
                  <b>{template.name}</b>
                  <small>{template.description}</small>
                </div>
              </label>
            ))}
          </div>
        )}

        <div>
          <button type="button" onClick={onClose}>取消</button>
          <button
            type="button"
            className="solid"
            disabled={loading || !selectedId || creating}
            onClick={create}
          >
            {creating ? '创建中…' : '确认创建'}
          </button>
        </div>
      </div>
    </div>
  )
}
