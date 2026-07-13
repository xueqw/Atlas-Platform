import { useEffect, useState } from 'react'
import { createOrResetApiKey, getApiKey, getDeployConfig, updateApiKey } from './api'
import type { Agent, ApiKey } from './types'

const statusLabel: Record<string, string> = {
  active: '启用',
  disabled: '停用',
}

function toDateInputValue(iso: string | null) {
  return iso ? iso.slice(0, 10) : ''
}

export default function AgentApiKeyDialog({
  agent,
  onClose,
  notice,
}: {
  agent: Agent
  onClose: () => void
  notice: (text: string) => void
}) {
  const [keyInfo, setKeyInfo] = useState<ApiKey | null>(null)
  const [plaintext, setPlaintext] = useState('')
  const [apiAccessEnabled, setApiAccessEnabled] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [expiresAt, setExpiresAt] = useState('')
  const [dailyQuota, setDailyQuota] = useState('')
  const [allowedOrigins, setAllowedOrigins] = useState('')
  const [showExample, setShowExample] = useState(false)

  useEffect(() => {
    Promise.all([getApiKey(agent.id), getDeployConfig(agent.id)])
      .then(([key, deployConfig]) => {
        setKeyInfo(key)
        setApiAccessEnabled(deployConfig.api_access)
        setExpiresAt(toDateInputValue(key.expires_at))
        setDailyQuota(key.daily_quota != null ? String(key.daily_quota) : '')
        setAllowedOrigins(key.allowed_origins)
      })
      .catch(() => notice('加载 API Key 失败'))
      .finally(() => setLoading(false))
  }, [agent.id])

  async function generate() {
    setBusy(true)
    try {
      const created = await createOrResetApiKey(agent.id)
      setKeyInfo(created)
      setPlaintext(created.key)
      setExpiresAt(toDateInputValue(created.expires_at))
      setDailyQuota(created.daily_quota != null ? String(created.daily_quota) : '')
      setAllowedOrigins(created.allowed_origins)
      notice('API Key 已生成')
    } catch (error) {
      notice(error instanceof Error ? error.message : '生成失败')
    } finally {
      setBusy(false)
    }
  }

  async function reset() {
    if (!confirm('重置后旧 Key 立即失效，确认继续？')) return
    await generate()
  }

  async function saveSettings() {
    if (!keyInfo) return
    setBusy(true)
    try {
      const updated = await updateApiKey(agent.id, {
        status: keyInfo.status,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        daily_quota: dailyQuota.trim() ? Number(dailyQuota) : null,
        allowed_origins: allowedOrigins,
      })
      setKeyInfo(updated)
      notice('已保存')
    } finally {
      setBusy(false)
    }
  }

  async function toggleStatus() {
    if (!keyInfo) return
    const nextStatus = keyInfo.status === 'active' ? 'disabled' : 'active'
    setBusy(true)
    try {
      const updated = await updateApiKey(agent.id, {
        status: nextStatus,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        daily_quota: dailyQuota.trim() ? Number(dailyQuota) : null,
        allowed_origins: allowedOrigins,
      })
      setKeyInfo(updated)
      notice(nextStatus === 'active' ? '已启用' : '已停用')
    } finally {
      setBusy(false)
    }
  }

  function copy(text: string) {
    navigator.clipboard?.writeText(text)
    notice('已复制')
  }

  const apiBase = window.location.origin

  return (
    <div className="run-drawer-mask" onClick={onClose}>
      <aside className="run-drawer api-key-dialog" onClick={e => e.stopPropagation()}>
        <header>
          <strong>API Key · {agent.name}</strong>
          <button className="rd-close" onClick={onClose}>×</button>
        </header>

        {loading ? (
          <p className="rd-empty">加载中…</p>
        ) : (
          <div className="deploy-config-body">
            {!apiAccessEnabled && (
              <p className="checklist-hint warn">
                落地配置未开启「允许外部 API 调用」，生成的 Key 暂时无法调用。请先在「落地配置」里打开该开关。
              </p>
            )}

            {plaintext && (
              <section className="deploy-config-section api-key-reveal">
                <h4>明文 Key<small>离开后无法再次查看，请立即复制保存</small></h4>
                <div className="api-key-plaintext-row">
                  <code>{plaintext}</code>
                  <button type="button" onClick={() => copy(plaintext)}>复制</button>
                </div>
              </section>
            )}

            {!keyInfo?.exists ? (
              <section className="deploy-config-section">
                <p className="rd-empty">尚未生成 API Key</p>
                <div className="deploy-config-actions">
                  <button className="solid" onClick={generate} disabled={busy || !apiAccessEnabled}>生成 API Key</button>
                </div>
              </section>
            ) : (
              <>
                <section className="deploy-config-section">
                  <h4>Key 信息</h4>
                  <div className="api-key-meta">
                    <span>{keyInfo.key_prefix}***</span>
                    <span className={`provider-status ${keyInfo.status === 'active' ? 'on' : 'off'}`}>
                      {keyInfo.status === 'active' ? '● ' : '○ '}
                      {statusLabel[keyInfo.status] || keyInfo.status}
                    </span>
                  </div>
                  <div className="api-key-meta">
                    <span>创建于 {keyInfo.created_at ? new Date(keyInfo.created_at).toLocaleString() : '—'}</span>
                    <span>最近使用 {keyInfo.last_used_at ? new Date(keyInfo.last_used_at).toLocaleString() : '从未使用'}</span>
                  </div>
                </section>

                <section className="deploy-config-section deploy-toggles">
                  <label className="deploy-toggle-line">
                    <input type="checkbox" checked={keyInfo.status === 'active'} onChange={toggleStatus} />
                    启用该 Key
                  </label>
                </section>

                <section className="deploy-config-section">
                  <h4>过期时间<small>留空=永不过期</small></h4>
                  <input type="date" value={expiresAt} onChange={e => setExpiresAt(e.target.value)} />
                </section>

                <section className="deploy-config-section">
                  <h4>每日调用额度<small>留空=不限</small></h4>
                  <input
                    type="number"
                    min={0}
                    value={dailyQuota}
                    onChange={e => setDailyQuota(e.target.value)}
                    placeholder="不限"
                  />
                </section>

                <section className="deploy-config-section">
                  <h4>允许来源<small>逗号分隔，留空=不限制</small></h4>
                  <textarea
                    value={allowedOrigins}
                    onChange={e => setAllowedOrigins(e.target.value)}
                    placeholder="https://example.com, https://app.example.com"
                  />
                </section>

                <div className="deploy-config-actions">
                  <button className="solid" onClick={saveSettings} disabled={busy}>{busy ? '保存中…' : '保存设置'}</button>
                  <button className="danger-link" onClick={reset} disabled={busy}>重置 Key</button>
                </div>

                <section className="deploy-config-section">
                  <button type="button" className="api-key-example-toggle" onClick={() => setShowExample(v => !v)}>
                    {showExample ? '收起调用示例' : '查看调用示例'}
                  </button>
                  {showExample && (
                    <pre className="api-key-example">
{`curl -X POST ${apiBase}/api/agents/${agent.id}/invoke \\
  -H "Authorization: Bearer <YOUR_API_KEY>" \\
  -H "Content-Type: application/json" \\
  -d '{"input": "你好"}'`}
                    </pre>
                  )}
                </section>
              </>
            )}
          </div>
        )}
      </aside>
    </div>
  )
}
