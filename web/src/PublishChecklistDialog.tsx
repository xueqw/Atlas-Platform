import { useEffect, useState } from 'react'
import { getPublishChecklist, publishAgent } from './api'
import type { Agent, PublishChecklist } from './types'

export default function PublishChecklistDialog({
  agent,
  onClose,
  onPublished,
  notice,
}: {
  agent: Agent
  onClose: () => void
  onPublished: () => void
  notice: (text: string) => void
}) {
  const [checklist, setChecklist] = useState<PublishChecklist | null>(null)
  const [loading, setLoading] = useState(true)
  const [publishing, setPublishing] = useState(false)

  useEffect(() => {
    getPublishChecklist(agent.id)
      .then(setChecklist)
      .catch(() => notice('加载发布检查清单失败'))
      .finally(() => setLoading(false))
  }, [agent.id])

  async function publish() {
    setPublishing(true)
    try {
      await publishAgent(agent.id)
      notice(`${agent.name} 已发布`)
      onPublished()
      onClose()
    } catch (error) {
      notice(error instanceof Error ? error.message : '发布失败')
    } finally {
      setPublishing(false)
    }
  }

  const hasWarningFailure = checklist?.items.some(item => item.level === 'warning' && !item.ok)

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <div className="dialog publish-checklist-dialog" onMouseDown={e => e.stopPropagation()}>
        <h2>发布前检查 · {agent.name}</h2>

        {loading || !checklist ? (
          <p className="rd-empty">检查中…</p>
        ) : (
          <>
            <ul className="checklist-items">
              {checklist.items.map(item => (
                <li key={item.key} className={item.ok ? 'pass' : item.level === 'blocking' ? 'fail' : 'warn'}>
                  <b>{item.ok ? '✓' : item.level === 'blocking' ? '✗' : '⚠'}</b>
                  {item.label}
                </li>
              ))}
            </ul>

            {!checklist.can_publish && (
              <p className="checklist-hint fail">还有必须项未通过，请先解决后再发布。</p>
            )}
            {checklist.can_publish && hasWarningFailure && (
              <p className="checklist-hint warn">仍有风险提醒未处理，确认要继续发布吗？</p>
            )}

            <div>
              <button type="button" onClick={onClose}>取消</button>
              <button
                type="button"
                className="solid"
                disabled={!checklist.can_publish || publishing}
                onClick={publish}
              >
                {publishing ? '发布中…' : '确认发布'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
