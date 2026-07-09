import { useEffect, useState } from 'react'
import { diffAgentVersions, getAgentVersion, listAgentVersions, rollbackAgentVersion, saveAgentVersion } from './api'
import type { Agent, AgentVersion, AgentVersionDetail, AgentVersionDiff } from './types'

const labelText: Record<string, string> = {
  draft: '草稿',
  published: '已发布',
  history: '历史',
}

export default function AgentVersionsDrawer({
  agent,
  onClose,
  onChanged,
  notice,
}: {
  agent: Agent
  onClose: () => void
  onChanged: () => void
  notice: (text: string) => void
}) {
  const [versions, setVersions] = useState<AgentVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string[]>([])
  const [detail, setDetail] = useState<AgentVersionDetail | null>(null)
  const [diff, setDiff] = useState<AgentVersionDiff | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = () =>
    listAgentVersions(agent.id)
      .then(list => {
        setVersions(list)
        if (list.length && selected.length === 0) setSelected([list[0].id])
      })
      .finally(() => setLoading(false))

  useEffect(() => {
    void refresh()
  }, [agent.id])

  useEffect(() => {
    setDetail(null)
    setDiff(null)
    if (selected.length === 1) {
      const version = versions.find(v => v.id === selected[0])
      if (version) getAgentVersion(agent.id, version.id).then(setDetail).catch(() => notice('读取版本详情失败'))
    } else if (selected.length === 2) {
      const [a, b] = selected.map(id => versions.find(v => v.id === id)).filter(Boolean) as AgentVersion[]
      if (a && b) {
        const [from, to] = a.version_no < b.version_no ? [a, b] : [b, a]
        diffAgentVersions(agent.id, from.version_no, to.version_no).then(setDiff).catch(() => notice('对比版本失败'))
      }
    }
  }, [selected, versions])

  function toggleSelect(id: string) {
    setSelected(old => {
      if (old.includes(id)) return old.filter(v => v !== id)
      if (old.length >= 2) return [old[1], id]
      return [...old, id]
    })
  }

  async function saveVersion() {
    setBusy(true)
    try {
      await saveAgentVersion(agent.id)
      notice('已保存新版本')
      await refresh()
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  async function rollback(version: AgentVersion) {
    if (!confirm(`回滚到 v${version.version_no}？当前草稿内容会被覆盖，并生成一条新的历史版本。`)) return
    setBusy(true)
    try {
      await rollbackAgentVersion(agent.id, version.id)
      notice(`已回滚到 v${version.version_no}`)
      await refresh()
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  function renderSnapshot() {
    if (!detail) return null
    if (detail.kind === 'code') {
      const files = (detail.snapshot.files as Record<string, string>) || {}
      return (
        <div className="version-snapshot code">
          {Object.keys(files).length === 0 && <p className="rd-empty">该版本没有文件快照</p>}
          {Object.entries(files).map(([path, content]) => (
            <details key={path} className="version-file">
              <summary>{path}</summary>
              <pre>{content}</pre>
            </details>
          ))}
        </div>
      )
    }
    return (
      <div className="version-snapshot prompt">
        <label>系统提示词<pre>{(detail.snapshot.system_prompt as string) || ''}</pre></label>
        <label>模型<span>{(detail.snapshot.model as string) || ''}</span></label>
      </div>
    )
  }

  function renderDiff() {
    if (!diff) return null
    if (diff.kind === 'code') {
      if (!diff.files.length) return <p className="rd-empty">两个版本文件内容一致</p>
      return (
        <div className="version-diff">
          {diff.files.map(file => (
            <details key={file.path} open className="version-file">
              <summary>
                <b className={`diff-status ${file.status}`}>{file.status}</b> {file.path}
              </summary>
              <pre className="version-diff-body">{file.diff}</pre>
            </details>
          ))}
        </div>
      )
    }
    if (!diff.fields.length) return <p className="rd-empty">两个版本配置一致</p>
    return (
      <div className="version-diff">
        {diff.fields.map(field => (
          <div className="version-field-diff" key={field.field}>
            <b>{field.field}</b>
            <div className="diff-old">- {String(field.old ?? '')}</div>
            <div className="diff-new">+ {String(field.new ?? '')}</div>
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="run-drawer-mask" onClick={onClose}>
      <aside className="run-drawer version-drawer" onClick={e => e.stopPropagation()}>
        <header>
          <strong>版本管理 · {agent.name}</strong>
          <button className="rd-close" onClick={onClose}>×</button>
        </header>

        <div className="version-drawer-toolbar">
          <span>选择 1 个查看内容，选择 2 个查看差异</span>
          <button className="solid" onClick={saveVersion} disabled={busy}>{busy ? '处理中' : '保存当前为新版本'}</button>
        </div>

        {loading ? (
          <p className="rd-empty">加载中…</p>
        ) : (
          <div className="rd-body version-body">
            <div className="rd-runs version-timeline">
              {versions.map(version => (
                <button
                  key={version.id}
                  className={`rd-run${selected.includes(version.id) ? ' on' : ''}`}
                  onClick={() => toggleSelect(version.id)}
                >
                  <i className={`rd-dot rd-${version.label === 'published' ? 'ok' : version.label === 'draft' ? 'run' : 'wait'}`} />
                  <span>
                    <b>v{version.version_no} · {labelText[version.label] || version.label}</b>
                    <small>{new Date(version.created_at).toLocaleString()}{version.note ? ` · ${version.note}` : ''}</small>
                  </span>
                  {version.label !== 'published' && (
                    <button
                      type="button"
                      className="version-rollback-btn"
                      onClick={e => {
                        e.stopPropagation()
                        void rollback(version)
                      }}
                    >
                      回滚
                    </button>
                  )}
                </button>
              ))}
              {versions.length === 0 && <p className="rd-empty">暂无版本记录</p>}
            </div>

            <div className="rd-detail version-detail">
              {selected.length === 2 ? renderDiff() : renderSnapshot()}
              {selected.length === 0 && <p className="rd-empty">选择一个版本查看内容</p>}
            </div>
          </div>
        )}
      </aside>
    </div>
  )
}
